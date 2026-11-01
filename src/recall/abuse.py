"""Checks that make the free trial less worth farming with throwaway accounts.

Two cheap, friction-light rules, applied only when a trial starts: the account's email must be verified,
and must not be on a throwaway-mail service. Neither stops a determined person (they can verify a real
second address); they remove the zero-effort cases. Card-on-file, phone or device limits would be stronger
and add friction, so they are left until abuse is actually seen.
"""

# A short list of well-known disposable-mail services. ponytail: not exhaustive and never will be; swap in a
# maintained list (e.g. the open-source "disposable-email-domains" package) if this needs to be thorough.
DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "sharklasers.com",
    "grr.la", "10minutemail.com", "10minutemail.net", "tempmail.com", "temp-mail.org", "temp-mail.io",
    "tempail.com", "trashmail.com", "trashmail.net", "yopmail.com", "yopmail.net", "getnada.com",
    "nada.email", "dispostable.com", "maildrop.cc", "mailnesia.com", "mintemail.com", "mohmal.com",
    "throwawaymail.com", "fakeinbox.com", "emailondeck.com", "mytemp.email", "tempinbox.com",
    "spambox.us", "spamgourmet.com", "burnermail.io", "inboxkitten.com", "moakt.com", "discard.email",
    "mailcatch.com", "tmail.ws", "tmpmail.org", "tmpmail.net", "1secmail.com", "1secmail.net",
    "emailfake.com", "fakemail.net", "mailtemp.net", "harakirimail.com", "33mail.com", "guerrillamailblock.com",
})


def is_disposable(email: str | None) -> bool:
    """True for an address on a disposable-mail domain (or a subdomain of one)."""
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].strip().lower()
    return any(domain == d or domain.endswith("." + d) for d in DISPOSABLE_DOMAINS)


def trial_refusal(email: str | None, email_verified: bool) -> str | None:
    """Why this account may not start the trial, in words for the user; None = allowed."""
    if not email_verified:
        return "Verify your email address to start the free trial."
    if is_disposable(email):
        return "Use a permanent email address to start the free trial."
    return None
