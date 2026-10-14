"""Import parsers for Venner: ICS birthdays, vCard/CSV contacts, phone lists."""
from __future__ import annotations

import csv
import io
import re

_BDAY_SUFFIX = re.compile(r"\s*(’s|'s|\bs)?\s*birthday.*$", re.IGNORECASE)
_BDAY_PREFIX = re.compile(r"^\s*(birthday|bday)\s*[:\-]\s*", re.IGNORECASE)
_DIGITS = re.compile(r"\d")


def split_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def clean_event_name(summary: str) -> str:
    name = _BDAY_PREFIX.sub("", (summary or "").strip())
    name = _BDAY_SUFFIX.sub("", name).strip().strip("\"'")
    return re.sub(r"\s+", " ", name)


def norm_date(raw: str) -> str:
    """Normalize birthday-ish values to YYYY-MM-DD or MM-DD when possible."""
    s = (raw or "").strip().replace("/", "-")
    if not s:
        return ""
    if "T" in s:
        s = s.split("T", 1)[0]
    p = [x for x in s.split("-") if x]
    if len(p) == 3 and len(p[0]) == 4:
        return f"{p[0]}-{p[1].zfill(2)}-{p[2].zfill(2)}"
    if len(p) == 2 and len(p[0]) <= 2:
        return f"{p[0].zfill(2)}-{p[1].zfill(2)}"
    digits = "".join(_DIGITS.findall(s))
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) == 4:
        return f"{digits[:2]}-{digits[2:]}"
    return s


def parse_ics_birthdays(text: str) -> list[dict]:
    """Extract birthday entries from an ICS calendar (VEVENT SUMMARY + DTSTART)."""
    out = []
    for block in re.split(r"BEGIN:VEVENT", text or "", flags=re.IGNORECASE)[1:]:
        body = block.split("END:VEVENT", 1)[0]
        summary, dtstart = "", ""
        for line in body.replace("\r\n", "\n").split("\n"):
            line = line.strip()
            low = line.lower()
            if low.startswith("summary"):
                summary = line.split(":", 1)[1].strip() if ":" in line else ""
            elif low.startswith("dtstart"):
                dtstart = line.split(":", 1)[1].strip() if ":" in line else ""
        name = clean_event_name(summary)
        if not name:
            continue
        first, last = split_name(name)
        out.append({"first_name": first, "last_name": last,
                    "birthday": norm_date(dtstart), "raw": summary})
    return out


def _unfold_vcard(text: str) -> list[str]:
    lines = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def parse_vcard(text: str) -> list[dict]:
    out, cur = [], None
    for line in _unfold_vcard(text):
        s = line.strip()
        low = s.lower()
        if low.startswith("begin:vcard"):
            cur = {"first_name": "", "last_name": "", "phone": "", "email": "",
                   "birthday": ""}
        elif low.startswith("end:vcard"):
            if cur and (cur["first_name"] or cur["last_name"] or cur["phone"]):
                out.append(cur)
            cur = None
        elif cur is not None and ":" in s:
            key, _, val = s.partition(":")
            key = key.split(";")[0].strip().lower()
            val = val.strip()
            if key == "fn" and val and not cur["first_name"]:
                cur["first_name"], cur["last_name"] = split_name(val)
            elif key == "n" and val:
                parts = [x.strip() for x in val.split(";")]
                last = parts[0] if len(parts) > 0 else ""
                first = parts[1] if len(parts) > 1 else ""
                if first or last:
                    cur["first_name"], cur["last_name"] = first, last
            elif key in ("tel",) and val and not cur["phone"]:
                cur["phone"] = val
            elif key == "email" and val and not cur["email"]:
                cur["email"] = val
            elif key == "bday" and val:
                cur["birthday"] = norm_date(val)
    return out


def _colkey(headers: list[str], *wants: str) -> str | None:
    norm = {h.strip().lower(): h for h in headers}
    for w in wants:
        if w in norm:
            return norm[w]
    for h in headers:
        low = h.strip().lower()
        if any(w in low for w in wants):
            return h
    return None


def parse_contacts_csv(text: str) -> list[dict]:
    out = []
    try:
        reader = csv.DictReader(io.StringIO(text or ""))
        headers = reader.fieldnames or []
    except csv.Error:
        return []
    if not headers:
        return []
    c_first = _colkey(headers, "first", "given", "firstname")
    c_last = _colkey(headers, "last", "surname", "family", "lastname")
    c_name = _colkey(headers, "name", "fullname", "contact")
    c_phone = _colkey(headers, "phone", "mobile", "tel", "number")
    c_email = _colkey(headers, "email", "mail")
    c_bday = _colkey(headers, "birthday", "bday", "dob", "birth")
    for row in reader:
        get = lambda c: (row.get(c) or "").strip() if c else ""
        first, last = get(c_first), get(c_last)
        if not (first or last) and c_name:
            first, last = split_name(get(c_name))
        if not (first or last or get(c_phone)):
            continue
        out.append({"first_name": first, "last_name": last, "phone": get(c_phone),
                    "email": get(c_email), "birthday": norm_date(get(c_bday))})
    return out


def parse_contacts(text: str, filename: str = "") -> list[dict]:
    low = (filename or "").lower()
    if low.endswith((".vcf", ".vcard")) or "begin:vcard" in (text or "").lower():
        return parse_vcard(text)
    return parse_contacts_csv(text)


def _looks_like_phone(part: str) -> bool:
    return len(_DIGITS.findall(part or "")) >= 6


def parse_phones(text: str) -> list[dict]:
    """Flexible phone-list import: CSV with headers, or name/phone per line."""
    out, seen = [], set()
    raw = (text or "").strip()
    if not raw:
        return []
    lines = [ln.strip() for ln in raw.replace("\r\n", "\n").split("\n") if ln.strip()]
    first = lines[0].lower() if lines else ""
    if ("," in first or ";" in first or "\t" in first) and any(
            k in first for k in ("phone", "mobile", "tel", "number", "name")):
        for row in parse_contacts_csv(raw):
            key = (row["first_name"].lower(), row["last_name"].lower(),
                   row["phone"].lower())
            if key not in seen:
                seen.add(key)
                out.append({"first_name": row["first_name"], "last_name": row["last_name"],
                            "phone": row["phone"]})
        return out
    for ln in lines:
        parts = [p.strip() for p in re.split(r"[,\t;|]", ln) if p.strip()]
        name, phone = "", ""
        if len(parts) >= 2:
            phones = [p for p in parts if _looks_like_phone(p)]
            names = [p for p in parts if p not in phones]
            phone = phones[0] if phones else ""
            name = " ".join(names)
        elif len(parts) == 1:
            if _looks_like_phone(parts[0]):
                phone = parts[0]
            else:
                name = parts[0]
        first, last = split_name(name)
        key = (first.lower(), last.lower(), phone.lower())
        if (first or last or phone) and key not in seen:
            seen.add(key)
            out.append({"first_name": first, "last_name": last, "phone": phone})
    return out
