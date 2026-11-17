"""Deterministic safety layer: payment-detail redaction and the client's always-human rules.

Nothing here depends on the model. Redaction runs before ticket text reaches the graph
(so the model, the checkpoint DB and the logs never see it) and again on everything we post.
"""
import re

# ---- payment details

_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_IBAN_CANDIDATE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b", re.I)
_CVC = re.compile(r"\b(CVC|CVV2?|CID|security code|beveiligingscode)\b\W{0,3}\d{3,4}\b", re.I)
_EXPIRY = re.compile(r"\b(geldig tot|vervaldatum|exp(?:iry|ires)?|gültig bis|valid (?:until|thru))\W{0,3}\d{1,2}\s*/\s*\d{2,4}", re.I)


def luhn_ok(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def iban_ok(raw):
    s = re.sub(r"\s", "", raw).upper()
    if not 15 <= len(s) <= 34:
        return False
    num = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(num) % 97 == 1


def redact(text):
    """Replace card numbers (Luhn-valid), IBANs (mod-97-valid), CVCs and expiry dates."""
    if not text:
        return text or ""

    def card(m):
        digits = re.sub(r"\D", "", m.group())
        return "[CARD REDACTED]" if 13 <= len(digits) <= 19 and luhn_ok(digits) else m.group()

    text = _CARD_CANDIDATE.sub(card, text)
    text = _IBAN_CANDIDATE.sub(lambda m: "[IBAN REDACTED]" if iban_ok(m.group()) else m.group(), text)
    text = _CVC.sub(r"\1 [REDACTED]", text)
    return _EXPIRY.sub(r"\1 [REDACTED]", text)


def redact_ticket(t):
    return {**t, "subject": redact(t.get("subject")), "body": redact(t.get("body"))}


# ---- client rules that always go to a human (nl / en / de)
# A backstop next to the model's own rule flags. A match can only turn a reply/close into a
# handoff, so a false positive costs a support agent one minute, never a wrong answer.

RULE_KEYWORDS = {
    "refund": r"terugbetal|terugstort|terugkrijgen|geld terug|refund|money back|rückerstatt|zurückerstatt"
              r"|dubbel afgeschreven|twee keer .{0,25}afgeschreven|charged twice|double charge|chargeback|storno",
    "cancellation": r"opzeg|zeggen .{0,15}op\b|beëindig|cancel|terminate|kündig",
    "personal_data": r"\bavg\b|\bgdpr\b|dsgvo|persoonsgegevens|inzageverzoek|personal data|personenbezogen"
                     r"|gegevens .{0,30}verwijder|delete my data|recht op vergetelheid",
    "security": r"gehackt|hacked|ingelogd .{0,40}(niet|wij|onbekend)|vreemde login|onbekende login|unknown login"
                r"|suspicious login|phishing|datalek|data breach|unauthori[sz]ed access",
    "legal": r"advocaat|jurist|rechtszaak|rechter|schadevergoeding|aansprakelijk|ingebrekestelling|lawyer|attorney"
             r"|legal action|\bsue\b|anwalt|schadensersatz|klage",
    "churn_threat": r"zeggen we op|stappen over|overstappen naar|switch to another|we('| wi)ll leave|kündigen wir",
}
_RULE_RES = {rule: re.compile(p, re.I) for rule, p in RULE_KEYWORDS.items()}


def rule_hits(ticket):
    text = f"{ticket.get('subject') or ''}\n{ticket.get('body') or ''}"
    return [rule for rule, rx in _RULE_RES.items() if rx.search(text)]


if __name__ == "__main__":  # self-check: python safety.py
    s = "kaart 4111 1111 1111 1111, geldig tot 09/28, CVC 123, iban NL91 ABNA 0417 1643 00, order 1234 5678"
    r = redact(s)
    assert "4111" not in r and "0417" not in r and "CVC 123" not in r and "09/28" not in r, r
    assert "1234 5678" in r  # not a card: too short
    assert redact("bel 0612345678") == "bel 0612345678"  # phone number survives
    assert rule_hits({"subject": "Opzeggen", "body": ""}) == ["cancellation"]
    assert "refund" in rule_hits({"body": "Er is twee keer €312 afgeschreven"})
    assert rule_hits({"body": "Hoe voeg ik een medewerker toe?"}) == []
    print("safety ok:", r)
