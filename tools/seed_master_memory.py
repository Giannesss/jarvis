r"""One-off data seed from the "JARVIS — MASTER USER MEMORY" document the
user wrote for Jarvis (identity, the AI-marketing business, hardware/
lifestyle/money context, and the vision for Jarvis itself).

Uses memory.save() -- the same function a real voice save goes through --
rather than raw SQL, so every row gets the same norm/tags/timestamps a
spoken "Θυμήσου ότι ..." would produce.

Three tables end up holding this, and none of them hold all of it:

- `profile` is a strict key/value table, ONE value per key (name, studies,
  job, city, age, school, a single catch-all preference -- see
  PROFILE_PATTERNS in jarvis/memory.py). The source document is far richer
  than seven keys, so only what fits is seeded here, and two real gaps are
  flagged rather than silently dropped or silently guessed at:

  - No column holds a date of birth, only "ηλικια" (age in years, a plain
    number) -- so an age computed from the 2008-02-21 birthdate is seeded
    instead, and it WILL go stale (it already needs recomputing every
    birthday, starting the next one). Fine for now, worth knowing.
  - "πολη" (city) is a single value, but the document gives two cities --
    Kifisia/Athens (home base) and Xanthi (university base) -- that the
    schema cannot hold at once. Seeding one would silently bury the other,
    so this script leaves "πολη" alone; say whichever one you want
    "Θυμήσου ότι μένω στο/στη ..." to answer with.
  - "ονομα" is left alone too if a row already exists (checked below) --
    "Σε λένε Γιάννη" already worked in the Phase 2 hand test, and
    overwriting a working verbatim capture with a different spelling for
    no real gain isn't worth the risk.

- `businesses` gets one row for the AI marketing agency: `note` is a
  single TEXT column with no length limit, so the business plan's shape
  (services, pricing tiers, financial goals, first-client strategy) fits
  as one descriptive paragraph -- this is exactly the "a description of
  each business" the roadmap's Phase 5 step 5 asks for.

- Everything else in the document -- the laptop/hardware research, the
  lifestyle context (fitness/basketball/gaming/food/Xanthi), the money/
  car context, and Jarvis's own target feature set and principles --
  has no matching table at all. It is seeded as `notes` rows, one per
  topic, tagged the same way a real save would be (MEMORY_TAGS has no
  "hardware"/"lifestyle"/"money" tag, so most of these land untagged but
  stay findable by keyword search, the same as any other untagged note).

Run once, locally, against the real database:
    .\.venv\Scripts\python.exe tools\seed_master_memory.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# Same fix tools/barge_probe.py (and the other seed scripts) already
# needed: running this file directly puts only its own directory on
# sys.path, not the project root, so "from jarvis import" fails with
# ModuleNotFoundError unless the root is added first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis import db, memory  # noqa: E402

BIRTHDATE = date(2008, 2, 21)


def _age_now() -> int:
    today = date.today()
    years = today.year - BIRTHDATE.year
    if (today.month, today.day) < (BIRTHDATE.month, BIRTHDATE.day):
        years -= 1
    return years


# --- profile: only what a single key/value row can actually hold -----------

PROFILE: dict[str, str] = {
    "σπουδεσ": "Μηχανικός Περιβάλλοντος στο Δημοκρίτειο Πανεπιστήμιο Θράκης",
    "σχολη": "το Δημοκρίτειο Πανεπιστήμιο Θράκης (ΔΠΘ), Ξάνθη",
    "ηλικια": str(_age_now()),
}


def _seed_profile(conn) -> None:
    for key, value in PROFILE.items():
        existing = conn.execute(
            "SELECT value FROM profile WHERE key = ?", (key,)
        ).fetchone()
        if existing is not None:
            print(f"  (ήδη υπάρχει) {key} = {existing['value']}")
            continue
        parsed = memory.Parsed(
            "profile", {"key": key, "value": value, "norm": memory.normalize(value)}
        )
        memory.save(parsed, conn)
        print(f"  + {key} = {value}")

    existing_name = conn.execute(
        "SELECT value FROM profile WHERE key = 'ονομα'"
    ).fetchone()
    if existing_name is None:
        parsed = memory.Parsed(
            "profile",
            {
                "key": "ονομα",
                "value": "Γιάννης Ροντόπουλος",
                "norm": memory.normalize("Γιάννης Ροντόπουλος"),
            },
        )
        memory.save(parsed, conn)
        print("  + ονομα = Γιάννης Ροντόπουλος")
    else:
        print(f"  (ήδη υπάρχει) ονομα = {existing_name['value']} -- δεν αλλάζει")

    print(
        "  (παραλείπεται) πολη -- το έγγραφο δίνει δύο πόλεις (Κηφισιά/Αθήνα"
        " και Ξάνθη) και το πεδίο χωράει μόνο μία· πες ποια θες με 'Θυμήσου"
        " ότι μένω στο/στη ...'"
    )


# --- businesses: one row, the note carries the whole plan -------------------

BUSINESS_NOTE = (
    "AI-powered marketing agency για μικρές/τοπικές ελληνικές επιχειρήσεις "
    "(καφέ, εστιατόρια, γυμναστήρια, τοπικά μαγαζιά, Airbnb/ξενοδοχεία). Η "
    "αξία δεν είναι να πουλάει 'AI περιεχόμενο' αλλά να φέρνει περισσότερους "
    "πελάτες, κρατήσεις και πωλήσεις μέσω content (reels, βίντεο, "
    "διαφημίσεις Meta/Google) και αυτοματισμών (landing pages, booking "
    "funnels, analytics). Πακέτα: Starter ~150-300€/μήνα, Growth "
    "~400-500+€/μήνα, Premium ~800+€/μήνα -- στόχος recurring revenue, όχι "
    "πώληση ωρών. Οικονομικός στόχος: ~10.000€ τον πρώτο χρόνο, κλιμάκωση "
    "προς ~10.000€/μήνα σε ~7 χρόνια. Στρατηγική: βρες τοπικές επιχειρήσεις "
    "με εμφανή κενά στο μάρκετινγκ, έρευνα της παρουσίας τους, χτίσε μια "
    "προσφορά, βρες τον πρώτο πληρωμένο πελάτη, κάνε case study, μετά "
    "αύξηση τιμών/τυποποίηση/αυτοματισμός."
)


def _seed_business(conn) -> None:
    norm = memory.normalize(f"επιχειρηση μαρκετινγκ {BUSINESS_NOTE}")
    existing = conn.execute(
        "SELECT id FROM businesses WHERE note LIKE ?", (f"%{BUSINESS_NOTE[:40]}%",)
    ).fetchone()
    if existing is not None:
        print(f"  (ήδη υπάρχει) business id={existing['id']}")
        return
    parsed = memory.Parsed(
        "businesses", {"name": "AI marketing", "note": BUSINESS_NOTE, "norm": norm}
    )
    row_id = memory.save(parsed, conn)
    print(f"  + business id={row_id}: AI marketing")


# --- notes: everything that has no table of its own -------------------------

NOTES: list[str] = [
    # Laptop / hardware research.
    (
        "Ψάχνει ASUS ROG Zephyrus G14 (μοντέλο 2025 GA403WR) ως βασικό "
        "laptop για πανεπιστήμιο/CAD/GIS/προγραμματισμό/AI/Jarvis/επιχείρηση/"
        "gaming: Ryzen AI 9 HX 370, RTX 5070 Ti 12GB, 32GB RAM, 1TB SSD, "
        "14'' 3K OLED 120Hz, ασημί/λευκό. Αρχική σκέψη ~2.400€, τώρα θέλει "
        "το χαμηλότερο ρεαλιστικό κόστος· πιθανό να το φέρει κάποιος από "
        "ΗΠΑ, οπότε Best Buy/Amazon/Newegg/Micro Center/B&H/Walmart/ASUS/"
        "eBay μετράνε, ανοιχτό κουτί/ανακατασκευασμένο αποδεκτό αρκεί να "
        "είναι όντως διαθέσιμο. Έχει επίσης σκεφτεί το G16 (ίδιο GPU/RAM, "
        "μεγαλύτερη οθόνη) αλλά προτιμά το G14 για φορητότητα/σχέδιο. "
        "Τρέχων desktop: Ryzen 5 5600X, RTX 2060, 32GB RAM, ~1.5TB SSD, "
        "Gigabyte B550 Gaming X. Θέλει καθαρή, λευκή/ασημί, μίνιμαλ "
        "αισθητική σαν MacBook, όχι επιθετικό RGB 'gaming room' look."
    ),
    # Lifestyle: fitness, basketball, gaming, food, Xanthi.
    (
        "Θέλει σταθερή προπόνηση στο πανεπιστήμιο για μυϊκή μάζα και "
        "αθλητική εμφάνιση, μακροπρόθεσμος στόχος όχι βραχυπρόθεσμη "
        "πρόκληση· έχει ψάξει τιμές γυμναστηρίων στην Ξάνθη. Παίζει "
        "μπάσκετ: 188εκ, σέντερ γκαρντ (shooting guard), έπαιξε νεανικές "
        "κατηγορίες στον Δούκα, είχε περίπου έναν χρόνο+ διακοπή και θέλει "
        "να ξαναμπεί (τοπικό μπάσκετ Ξάνθη, φοιτητικές ομάδες), ακολουθεί "
        "τον Παναθηναϊκό ΚΑΕ. Gaming: κυρίως Rust (EU servers, ~300-600 "
        "population, Δευτέρα/Πέμπτη wipes σε ελληνική ώρα), ενδιαφέρον για "
        "GTA 6, ARK, Raiders, Bodycam. Μαγειρεύει μόνος του στην Ξάνθη: "
        "προτιμά πρωτεϊνούχα, γεμιστικά, εύκολα γεύματα στο air fryer "
        "(κοτόπουλο, ρύζι, πάστα καρμπονάρα, πατάτες, BBQ σάλτσες, τόνος, "
        "κάρι), αποφεύγει Worcestershire και άσκοπο λάδι/κέτσαπ. Ζει γύρω "
        "από ΔΠΘ/Κιμμέρια/Λεύκιππο/κέντρο Ξάνθης/Παλιά Πόλη."
    ),
    # Money / car.
    (
        "Είναι προσεκτικός με τα χρήματα: θέλει πραγματικές προσφορές, όχι "
        "ψεύτικες τιμές, καμία άσκοπη συνδρομή. Σκέφτεται ~350€/μήνα για "
        "βασικά φοιτητικά έξοδα (οι γονείς καλύπτουν μέρος), ώστε τα δικά "
        "του χρήματα να πηγαίνουν σε επιχείρηση/τεχνολογία/επενδύσεις. "
        "Μακροπρόθεσμο οικονομικό σχέδιο: πανεπιστήμιο -> τεχνικές "
        "δεξιότητες -> επιχείρηση AI/μάρκετινγκ -> επανεπένδυση -> "
        "κλιμακούμενα συστήματα -> επενδύσεις -> οικονομική ανεξαρτησία. "
        "Μακροπρόθεσμη (όχι άμεση) επιθυμία για ακριβό αυτοκίνητο με "
        "εισόδημα από την επιχείρηση, προϋπολογισμός ~100.000-150.000€, "
        "ίσως 50/50 με τον αδερφό του -- Porsche Cayman GT4/911 Carrera, "
        "Audi R8/RSQ8, Corvette C8 Z51, BMW M4/8-Series, Mercedes-AMG GT/"
        "G-Class, Range Rover, Porsche Cayenne Turbo, Aston Martin Vantage."
    ),
    # Jarvis's own target feature set and principles.
    (
        "Το όραμα για τον Jarvis: δομημένη μνήμη (όχι ένα τεράστιο αρχείο "
        "κειμένου) που θυμάται στόχους, projects, προτιμήσεις, αποφάσεις, "
        "πανεπιστήμιο, επιχείρηση· φωνή (είσοδος/έξοδος, hands-free, "
        "φυσικός διάλογος)· έλεγχο υπολογιστή (άνοιγμα εφαρμογών, αρχεία, "
        "κώδικας, scripts) και βοήθεια στην επιχείρηση (εύρεση/έρευνα "
        "πελατών, lead lists, CRM, πρότζεκτ, προτάσεις)· email/μηνύματα "
        "μόνο με ρητή άδεια. Αρχές: local-first όπου γίνεται (προτιμά να "
        "ελέγχει τα δεδομένα/δυνατότητες παρά να εξαρτάται από συνδρομές/"
        "cloud)· άδεια πριν από σημαντικές ενέργειες (αποστολή μηνυμάτων, "
        "χρήματα, διαγραφή αρχείων, δημοσιεύσεις) με σύντομη εξήγηση τι/"
        "γιατί/αποτέλεσμα· να λέει 'δεν ξέρω' αντί να επινοεί, και να "
        "ερευνά όταν χρειάζεται τρέχουσα πληροφορία· να σκέφτεται "
        "προληπτικά τι πρέπει να γίνει, τι ξεχνάει, τι μπλοκάρει ένα "
        "project, τι έχει τη μεγαλύτερη απόδοση."
    ),
    # How Jarvis/Claude should reason and communicate (mirrors the account
    # preferences.md entry, kept here too so a keyword search inside
    # jarvis.db itself -- not just Claude's own memory -- can find it).
    (
        "Θέλει συγκρίσεις όταν παρουσιάζονται επιλογές (τιμή, πλεονεκτήματα/"
        "μειονεκτήματα, κρυφά μειονεκτήματα, μακροπρόθεσμη αξία), έρευνα "
        "στο ίντερνετ για τρέχουσες τιμές/διαθεσιμότητα αντί για παλιά "
        "γνώση, και διάκριση νέο/ανοιχτό κουτί/ανακατασκευασμένο/"
        "μεταχειρισμένο. Θέλει τον Jarvis να ακούγεται έξυπνος, ήρεμος, "
        "άμεσος, πρακτικός -- όχι ρομποτικός ή υπερβολικά τυπικός, "
        "σύντομος σε απλές ερωτήσεις και αναλυτικός σε σημαντικές "
        "αποφάσεις. Δεν θέλει γενικές συμβουλές σαν 'ξεκίνα μικρό' χωρίς "
        "συγκεκριμένο λόγο, ή προτάσεις για άσκοπες συνδρομές -- θέλει την "
        "ερώτηση 'τι φέρνει τη μεγαλύτερη πρόοδο ανά ώρα/ευρώ'."
    ),
]


def _seed_notes(conn) -> None:
    for text in NOTES:
        norm = memory.normalize(text)
        existing = conn.execute(
            "SELECT id FROM notes WHERE norm = ?", (norm,)
        ).fetchone()
        if existing is not None:
            print(f"  (ήδη υπάρχει) note id={existing['id']}: {text[:40]}...")
            continue
        parsed = memory.Parsed("notes", {"text": text, "norm": norm})
        row_id = memory.save(parsed, conn)
        print(f"  + note id={row_id}: {text[:40]}...")


def main() -> None:
    conn = db.connect()
    try:
        print("Profile:")
        _seed_profile(conn)
        print("\nBusiness:")
        _seed_business(conn)
        print("\nNotes:")
        _seed_notes(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
