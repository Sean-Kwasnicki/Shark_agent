"""
Self-contained SMTP mailer — ported from the Pounce project (src/notify/mailer.py),
where it is proven by real-socket tests (a live in-process SMTP server, not mocks).

PORTABLE BY DESIGN: imports ONLY the Python standard library.

What it is:
  A robust SMTP client with retries, transient/permanent error classification,
  sliding-window rate limiting (stays inside free-tier provider quotas), a
  background send queue, dead-letter capture, and a CLI self-test. It speaks
  standard SMTP, so it works with ANY provider — including a free personal
  Gmail account via an App Password ($0/month).

What it is NOT (honest limits):
  * Not a mail SERVER. Direct-to-MX from residential IPs without SPF/DKIM/
    reverse-DNS gets rejected or spam-foldered, so this relays through a real
    SMTP endpoint instead.
  * Free Gmail caps sending at ~500 recipients per rolling 24h and requires an
    App Password (2-Step Verification enabled; regular passwords rejected).
    The rate limiter defaults WELL under those caps.

Gmail quick start ($0):
  1. Enable 2-Step Verification on a Google account.
  2. Google Account -> Security -> App Passwords -> create one for "Mail".
  3. Set env vars:
       SMTP_HOST=smtp.gmail.com
       SMTP_PORT=587
       SMTP_SECURITY=starttls
       SMTP_USER=youraddress@gmail.com
       SMTP_PASSWORD=<16-char app password>
       SMTP_FROM=youraddress@gmail.com
       SMTP_FROM_NAME=Shark
  4. Self-test:  python -m agent.tools.mailer --test you@example.com

NOTE: the agent's outreach lane (agent/outreach.py) is the only caller; it
adds CAN-SPAM compliance, suppression, and owner-approval gating on top.
"""
from __future__ import annotations

import logging
import os
import queue
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

log = logging.getLogger("mailer")

# Ports 587 (STARTTLS) and 465 (implicit SSL) are the standard submission
# ports; 25 is unencrypted relay and most ISPs/providers block it.
_SECURITY_MODES = ("starttls", "ssl", "none")


@dataclass
class MailerConfig:
    host: str = ""
    port: int = 587
    security: str = "starttls"        # "starttls" | "ssl" | "none" (tests only)
    username: str = ""
    password: str = ""
    from_addr: str = ""
    from_name: str = ""
    timeout: float = 15.0
    # Retries: transient failures (4xx, disconnects, timeouts) are retried
    # with exponential backoff; permanent failures (5xx, bad auth) are not.
    max_attempts: int = 3
    backoff_seconds: float = 2.0
    # Rate limits. Defaults sit safely under free-Gmail's ~500 recipients per
    # rolling 24h and ~100/hour burst throttling. Raise only with evidence
    # your provider allows it.
    max_per_day: int = 400
    max_per_hour: int = 60

    @classmethod
    def from_env(cls, prefix: str = "SMTP_") -> "MailerConfig":
        e = os.getenv
        return cls(
            host=e(f"{prefix}HOST", ""),
            port=int(e(f"{prefix}PORT", "587")),
            security=e(f"{prefix}SECURITY", "starttls").lower(),
            username=e(f"{prefix}USER", ""),
            password=e(f"{prefix}PASSWORD", ""),
            from_addr=e(f"{prefix}FROM", e(f"{prefix}USER", "")),
            from_name=e(f"{prefix}FROM_NAME", ""),
            max_per_day=int(e(f"{prefix}MAX_PER_DAY", "400")),
            max_per_hour=int(e(f"{prefix}MAX_PER_HOUR", "60")),
        )

    def validate(self) -> list[str]:
        problems = []
        if not self.host:
            problems.append("host is empty")
        if not self.from_addr:
            problems.append("from_addr is empty")
        if self.security not in _SECURITY_MODES:
            problems.append(f"security must be one of {_SECURITY_MODES}")
        return problems


@dataclass
class SendResult:
    ok: bool
    attempts: int
    error: str = ""


@dataclass
class DeadLetter:
    to: str
    subject: str
    error: str
    attempts: int
    failed_at: float = field(default_factory=time.time)


class _RateLimiter:
    """Sliding-window limiter. Clock is injectable so tests are deterministic."""

    def __init__(self, max_per_hour: int, max_per_day: int, clock=time.monotonic):
        self.max_per_hour = max_per_hour
        self.max_per_day = max_per_day
        self._clock = clock
        self._sends: list[float] = []
        self._lock = threading.Lock()

    def try_acquire(self) -> tuple[bool, str]:
        now = self._clock()
        with self._lock:
            self._sends = [t for t in self._sends if now - t < 86400]
            if len(self._sends) >= self.max_per_day:
                return False, f"daily send limit reached ({self.max_per_day}/24h)"
            last_hour = sum(1 for t in self._sends if now - t < 3600)
            if last_hour >= self.max_per_hour:
                return False, f"hourly send limit reached ({self.max_per_hour}/h)"
            self._sends.append(now)
            return True, "ok"

    def seconds_until_slot(self) -> float:
        """How long until a send would be allowed (for the queue worker)."""
        now = self._clock()
        with self._lock:
            active = [t for t in self._sends if now - t < 86400]
            if len(active) >= self.max_per_day:
                return max(0.0, 86400 - (now - min(active)) + 0.01)
            hour = sorted(t for t in active if now - t < 3600)
            if len(hour) >= self.max_per_hour:
                return max(0.0, 3600 - (now - hour[0]) + 0.01)
            return 0.0


def _is_transient(exc: Exception) -> bool:
    """Retry 4xx/disconnect/timeout; never retry 5xx or bad credentials."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return False
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        # per-recipient dict of (code, msg); transient only if all 4xx
        return all(400 <= code < 500 for code, _ in exc.recipients.values())
    if isinstance(exc, (smtplib.SMTPServerDisconnected, ConnectionError,
                        TimeoutError, OSError)):
        return True
    return False


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (f"authentication rejected ({exc.smtp_code}): check username and "
                "use an APP PASSWORD, not the account password (Gmail requires "
                "2-Step Verification + App Password)")
    return f"{type(exc).__name__}: {exc}"


class Mailer:
    def __init__(self, config: MailerConfig, clock=time.monotonic):
        self.config = config
        self.limiter = _RateLimiter(config.max_per_hour, config.max_per_day, clock)
        self.dead_letters: list[DeadLetter] = []
        self.sent_count = 0
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._stats_lock = threading.Lock()

    # ------------------------------------------------------------ build
    def _build_message(self, to: str, subject: str, text: str,
                       html: str | None) -> EmailMessage:
        msg = EmailMessage()
        cfg = self.config
        msg["From"] = (formataddr((cfg.from_name, cfg.from_addr))
                       if cfg.from_name else cfg.from_addr)
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        return msg

    # ---------------------------------------------------------- deliver
    def _deliver(self, msg: EmailMessage) -> None:
        """One real SMTP conversation. Raises on any failure."""
        cfg = self.config
        context = ssl.create_default_context()
        if cfg.security == "ssl":
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout,
                                      context=context)
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
        try:
            server.ehlo()
            if cfg.security == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if cfg.username:
                server.login(cfg.username, cfg.password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass    # delivery already succeeded/failed; QUIT is courtesy

    # ----------------------------------------------------------- public
    def send_now(self, to: str, subject: str, text: str,
                 html: str | None = None,
                 _skip_rate_check: bool = False) -> SendResult:
        """Blocking send with retries. Returns an honest result — ok=True
        means the SMTP server ACCEPTED the message, nothing less."""
        problems = self.config.validate()
        if problems:
            return SendResult(False, 0, f"config invalid: {'; '.join(problems)}")
        if not _skip_rate_check:
            allowed, why = self.limiter.try_acquire()
            if not allowed:
                return SendResult(False, 0, why)

        msg = self._build_message(to, subject, text, html)
        last_err = ""
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                self._deliver(msg)
                with self._stats_lock:
                    self.sent_count += 1
                log.info("sent to=%s subject=%r attempt=%d", to, subject, attempt)
                return SendResult(True, attempt)
            except Exception as exc:                      # noqa: BLE001
                last_err = _friendly_error(exc)
                log.warning("send failed to=%s attempt=%d: %s",
                            to, attempt, last_err)
                if not _is_transient(exc) or attempt == self.config.max_attempts:
                    break
                time.sleep(self.config.backoff_seconds * (2 ** (attempt - 1)))
        self.dead_letters.append(
            DeadLetter(to=to, subject=subject, error=last_err, attempts=attempt))
        return SendResult(False, attempt, last_err)

    def send(self, to: str, subject: str, text: str, html: str | None = None):
        """Fire-and-forget: enqueue for the background worker. Failures land
        in dead_letters. Call start() once before using."""
        self._queue.put((to, subject, text, html))

    # ------------------------------------------------------ queue worker
    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="mailer-worker")
        self._worker.start()

    def stop(self, drain_timeout: float = 10.0) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=drain_timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            # Respect the rate limit by WAITING (queued mail is not urgent
            # enough to drop, and dropping would be dishonest to callers).
            while not self._stop.is_set():
                wait = self.limiter.seconds_until_slot()
                if wait <= 0:
                    break
                time.sleep(min(wait, 1.0))
            allowed, _ = self.limiter.try_acquire()
            if not allowed:      # stop() raced us; requeue and exit
                self._queue.put(item)
                return
            to, subject, text, html = item
            self.send_now(to, subject, text, html, _skip_rate_check=True)

    # ------------------------------------------------------- diagnostics
    def test_connection(self) -> tuple[bool, str]:
        """Connect + EHLO + (login if creds). Sends nothing. For setup checks."""
        problems = self.config.validate()
        if problems:
            return False, f"config invalid: {'; '.join(problems)}"
        try:
            cfg = self.config
            context = ssl.create_default_context()
            if cfg.security == "ssl":
                server = smtplib.SMTP_SSL(cfg.host, cfg.port,
                                          timeout=cfg.timeout, context=context)
            else:
                server = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
            try:
                server.ehlo()
                if cfg.security == "starttls":
                    server.starttls(context=context)
                    server.ehlo()
                if cfg.username:
                    server.login(cfg.username, cfg.password)
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
            return True, "connection + authentication OK"
        except Exception as exc:                          # noqa: BLE001
            return False, _friendly_error(exc)


# ---------------------------------------------------------------------------
# CLI self-test:  python -m agent.tools.mailer --test someone@example.com
# Reads config from SMTP_* environment variables.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Mailer self-test")
    parser.add_argument("--test", metavar="TO",
                        help="send a real test email to this address")
    parser.add_argument("--check", action="store_true",
                        help="only test connection/auth; send nothing")
    args = parser.parse_args()

    cfg = MailerConfig.from_env("SMTP_")
    print(f"config: host={cfg.host!r} port={cfg.port} security={cfg.security} "
          f"user={cfg.username!r} from={cfg.from_addr!r}")
    mailer = Mailer(cfg)

    if args.check or not args.test:
        ok, detail = mailer.test_connection()
        print(("OK: " if ok else "FAILED: ") + detail)
        raise SystemExit(0 if ok else 1)

    res = mailer.send_now(
        args.test, "Shark mailer self-test",
        "If you can read this, the self-hosted mailer works end to end.",
        html="<p>If you can read this, the <b>self-hosted mailer</b> works "
             "end to end.</p>")
    print(f"ok={res.ok} attempts={res.attempts} error={res.error!r}")
    raise SystemExit(0 if res.ok else 1)
