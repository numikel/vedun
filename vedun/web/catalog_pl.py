"""Polish translations of the dashboard texts (see :mod:`vedun.web.i18n`).

Keys are the English source strings exactly as they appear in the templates, ``web/*.py`` and ``app.js``.
``tests/test_web_i18n.py`` scans those sources and fails when a text has no entry here or an entry is stale.
"""

from __future__ import annotations

#: English source -> Polish
PL: dict[str, str] = {
    # ---- navigation, header, shell
    "Overview": "Przegląd",
    "Documents": "Dokumenty",
    "Search": "Wyszukiwarka",
    "Sync": "Synchronizacja",
    "Citations in repo": "Cytaty w repo",
    "MCP connection": "Połączenie MCP",
    "By number": "Po numerze",
    "Full text": "Pełnotekstowe",
    "Hybrid": "Hybrydowe",
    "Skip to content": "Przejdź do treści",
    "Main navigation": "Nawigacja główna",
    "vedun, home page": "vedun, strona główna",
    "Interface language": "Język interfejsu",
    "Switch between light and dark theme": "Przełącz motyw jasny i ciemny",
    "Switch theme": "Przełącz motyw",
    "Theme": "Motyw",
    "Dark theme": "Motyw ciemny",
    "Light theme": "Motyw jasny",
    "Local data. Cite only what the database returns.": "Dane lokalne. Cytuj tylko to, co zwraca baza.",
    "Image preview": "Podgląd obrazu",
    "Show page": "Pokaż stronę",
    "Show figure": "Pokaż rysunek",
    "Open unit": "Otwórz jednostkę",
    "Close preview": "Zamknij podgląd",
    "Loading image…": "Ładowanie obrazu…",
    # ---- shared widgets
    "Copy": "Kopiuj",
    "Copy to clipboard": "Kopiuj do schowka",
    "Previous": "Poprzednie",
    "Next": "Następne",
    "Result pages": "Paginacja wyników",
    "Pagination": "Paginacja",
    "Results {first}–{last}": "Wyniki {first}–{last}",
    "Results {first}–{last} of {total}": "Wyniki {first}–{last} z {total}",
    "Rows {first}–{last} of {total}": "Wiersze {first}–{last} z {total}",
    "Back to the overview": "Wróć do przeglądu",
    "OK": "OK",
    "All": "Wszystkie",
    "Filter": "Filtruj",
    "Title": "Tytuł",
    "Edition": "Edycja",
    "Edition:": "Edycja:",
    "Status": "Status",
    "Start": "Start",
    "Duration": "Czas",
    "Message": "Komunikat",
    "Actions": "Akcje",
    "Details": "Szczegóły",
    "Units": "Jednostki",
    "of": "z",
    "yes": "tak",
    "no": "nie",
    "none": "brak",
    "None.": "Brak.",
    # ---- kinds and statuses (labels kept in web/data.py)
    "Clause": "Punkt",
    "Table": "Tabela",
    "Figure": "Rysunek",
    "Appendix": "Załącznik",
    "Clauses": "Punkty",
    "Tables": "Tabele",
    "Figures": "Rysunki",
    "Appendices": "Załączniki",
    "All kinds": "Wszystkie rodzaje",
    "Tab.": "Tab.",
    "Fig.": "Rys.",
    "App.": "Zał.",
    "Current": "Aktualny",
    "Needs refresh": "Do odświeżenia",
    "Not local": "Brak lokalnie",
    "Withdrawn": "Wycofany",
    "No data": "Brak danych",
    "Document sync": "Synchronizacja dokumentów",
    "Citation check": "Sprawdzanie cytowań",
    "Catalog refresh": "Odświeżenie katalogu",
    "Queued": "W kolejce",
    "Running": "W toku",
    "Done": "Zakończone",
    "Failed": "Błąd",
    "Cancelled": "Anulowane",
    "UNKNOWN CLAUSE": "NIEZNANY PUNKT",
    "DOCUMENT NOT INDEXED": "DOKUMENT NIEZINDEKSOWANY",
    "UNKNOWN": "NIEZNANY",
    "Unknown clause": "Nieznany punkt",
    "Document not indexed": "Dokument niezindeksowany",
    "None (faster, no cost)": "Brak (szybciej, bez kosztu)",
    # ---- overview
    "Local knowledge base: {corpus}.": "Lokalna baza wiedzy: {corpus}.",
    "Quick search": "Szybkie wyszukiwanie",
    "e.g. {example}": "np. {example}",
    "Ask a question in your own words": "Zadaj pytanie własnymi słowami",
    "Hybrid search (BM25 + vectors). Works in Polish and English.": (
        "Wyszukiwanie hybrydowe (BM25 + wektory). Działa po polsku i po angielsku."
    ),
    "Hybrid search (BM25 + vectors). Works in Polish and English. A document and clause number in the query "
    "(e.g. {example}) gives an exact hit.": (
        "Wyszukiwanie hybrydowe (BM25 + wektory). Działa po polsku i po angielsku. Numer dokumentu i punktu "
        "w zapytaniu (np. {example}) daje trafienie dokładne."
    ),
    "Indicators": "Wskaźniki",
    "indexed, one edition each": "zindeksowane, po jednej edycji",
    "numbered requirements": "ponumerowane wymagania",
    "with the image cut out": "z wyciętym obrazem",
    "Sync status": "Stan synchronizacji",
    "Last sync": "Ostatnia synchronizacja",
    "from jobs, reports or the indexing date": "wg zadań, raportów lub daty indeksowania",
    "not run yet": "jeszcze nie uruchamiano",
    "Freshness against {source}": "Aktualność względem {source}",
    "No catalog data": "Brak danych katalogu",
    "Refresh the catalog on the {page} page to compare editions with {source}.": (
        "Odśwież katalog na stronie {page}, aby sprawdzić aktualność edycji względem {source}."
    ),
    "API cost": "Koszt API",
    "embeddings and reranking combined": "embeddingi i reranking łącznie",
    "cost counter unavailable": "licznik kosztów niedostępny",
    "Recent changes": "Ostatnie zmiany",
    "All reports": "Wszystkie raporty",
    "no changes": "bez zmian",
    "No change reports": "Brak raportów zmian",
    "A report is written after a sync that added or replaced a document. Files named sync_YYYY-MM-DD.md land in "
    "the reports folder.": (
        "Raport powstaje po synchronizacji, która dodała lub zastąpiła dokument. Pliki sync_RRRR-MM-DD.md trafiają "
        "do katalogu raportów."
    ),
    "Recent jobs": "Ostatnie zadania",
    "History": "Historia",
    "No job has been run yet": "Nie uruchomiono jeszcze żadnego zadania",
    "Jobs (sync, citation check, catalog refresh) will appear here with their progress and status.": (
        "Zadania (synchronizacja, sprawdzanie cytowań, odświeżenie katalogu) pojawią się tutaj z postępem i statusem."
    ),
    "Jobs are unavailable": "Zadania są niedostępne",
    "The jobs module is not loaded in this instance. Browsing and search work normally.": (
        "Moduł zadań nie jest załadowany w tej instancji. Przeglądanie i wyszukiwanie działają normalnie."
    ),
    "Number": "Numer",
    "Indexed documents": "Zindeksowane dokumenty",
    "See all": "Zobacz wszystkie",
    "The database is empty": "Baza jest pusta",
    "Add PDF files on the Sync page to build the index.": (
        "Dodaj pliki PDF na stronie Synchronizacja, aby zbudować indeks."
    ),
    # ---- documents list
    "All documents in the database, with their edition and status against {source}.": (
        "Wszystkie dokumenty w bazie, z edycją i stanem względem {source}."
    ),
    "Filter by number or title": "Filtruj po numerze lub tytule",
    "e.g. a number or a word from the title": "np. numer albo słowo z tytułu",
    "Status against {source}": "Stan względem {source}",
    "Status per {source}": "Stan wg {source}",
    "The status against {source} is unknown because the catalog has not been fetched yet. {link}.": (
        "Stan względem {source} jest nieznany, bo katalog nie został jeszcze pobrany. {link}."
    ),
    "Refresh the catalog": "Odśwież katalog",
    "on {source}: {edition}": "wg {source}: {edition}",
    "the source": "źródła",
    "No documents match the filter": "Brak dokumentów pasujących do filtra",
    "Change the phrase or clear the status filter.": "Zmień frazę albo wyczyść filtr stanu.",
    "Clear filters": "Wyczyść filtry",
    "The database has no documents yet": "Baza nie zawiera jeszcze dokumentów",
    "Add PDF files on the Sync page. The documents will appear here once processed.": (
        "Dodaj pliki PDF na stronie Synchronizacja. Dokumenty pojawią się tutaj po przetworzeniu."
    ),
    "Go to sync": "Przejdź do synchronizacji",
    # ---- document detail
    "Breadcrumb": "Ścieżka",
    "On {source}: {edition}": "Wg {source}: {edition}",
    "PDF pages": "Stron PDF",
    "Vector chunks": "Fragmentów wektorowych",
    "Indexed": "Zindeksowano",
    "Source file": "Plik źródłowy",
    "Embeddings": "Embeddingi",
    "Download Markdown": "Pobierz Markdown",
    "Copy MD path": "Kopiuj ścieżkę MD",
    "Copy the path of the Markdown file on your computer": "Kopiuj ścieżkę pliku Markdown na komputerze",
    "Open PDF (original)": "Otwórz PDF (oryginał)",
    "Copy PDF path": "Kopiuj ścieżkę PDF",
    "Copy the path of the PDF file on your computer": "Kopiuj ścieżkę pliku PDF na komputerze",
    "Document view": "Widok dokumentu",
    "Structure": "Struktura",
    "Table of contents of the document": "Spis treści dokumentu",
    "Table of contents": "Spis treści",
    "Click an entry to see its text. The arrow expands sub-clauses.": (
        "Kliknij pozycję, aby zobaczyć tekst. Strzałka rozwija podpunkty."
    ),
    "Selected unit": "Wybrana jednostka",
    "Select a clause, table or figure": "Wybierz punkt, tabelę lub rysunek",
    "Pick an entry from the table of contents to see its verbatim text, a ready-to-paste citation (PL and EN) and "
    "the quote-checking tool.": (
        "Po wybraniu pozycji ze spisu treści zobaczysz tu dosłowny tekst, gotowe cytowanie (PL i EN) i narzędzie "
        "do sprawdzania cytatów."
    ),
    "Figure gallery": "Galeria rysunków",
    "No image": "Brak obrazu",
    "PDF page": "strona PDF",
    "show page": "pokaż stronę",
    "This document has no figures": "Ten dokument nie ma rysunków",
    "None of the units of this document is a figure.": "Żadna jednostka tego dokumentu nie jest rysunkiem.",
    "Expand or collapse the sub-clauses of {id}": "Rozwiń lub zwiń podpunkty {id}",
    "No entries.": "Brak pozycji.",
    # ---- evidence card
    "Position in the results": "Pozycja w wynikach",
    "Position": "Pozycja",
    "Exact match by number": "Trafienie dokładne po numerze",
    "Number repeated in the source": "Powtórzony numer w źródle",
    "Location in the document": "Położenie w dokumencie",
    "Copy the Polish citation": "Kopiuj cytowanie po polsku",
    "Copy PL": "Kopiuj PL",
    "Copy the English citation": "Kopiuj cytowanie po angielsku",
    "Copy EN": "Kopiuj EN",
    "Enlarge": "Powiększ",
    "Matched fragment": "Dopasowany fragment",
    "Original text (verbatim)": "Tekst oryginalny (dosłowny)",
    "Show all": "Pokaż całość",
    "Collapse": "Zwiń",
    "Open the full text": "Otwórz pełny tekst",
    "Showing {shown} of {total} characters. {link}.": "Pokazano {shown} z {total} znaków. {link}.",
    "Why this result": "Dlaczego ten wynik",
    "The document and clause number was recognized in the query, so the unit is placed first without using "
    "vectors or the reranker.": (
        "Numer dokumentu i punktu został rozpoznany w zapytaniu, dlatego jednostka trafia na pierwsze miejsce "
        "bez użycia wektorów i rerankera."
    ),
    "Final method": "Metoda końcowa",
    "rank {n}": "miejsce {n}",
    "Vectors {model}": "Wektory {model}",
    "similarity {value}": "podobieństwo {value}",
    "distance {value}": "odległość {value}",
    "RRF (fusion)": "RRF (fuzja)",
    "weights BM25 {w_fts} / vectors {w_vec}": "wagi BM25 {w_fts} / wektory {w_vec}",
    "The reranker reported an error, the fusion order was kept: {error}": (
        "Reranker zgłosił błąd, zachowano kolejność z fuzji: {error}"
    ),
    "show PDF page": "pokaż stronę PDF",
    "Check a quote": "Sprawdź cytat",
    "Quote to check": "Cytat do sprawdzenia",
    "Paste a quote from the document (in English, verbatim) to check it against this unit and the PDF…": (
        "Wklej cytat z dokumentu (po angielsku, dosłownie), aby sprawdzić go w tej jednostce i w PDF…"
    ),
    "Check": "Sprawdź",
    "Checking the text and the PDF…": "Sprawdzam tekst i PDF…",
    # ---- lookup item
    "Neighboring units": "Sąsiednie jednostki",
    "previous": "poprzednia",
    "next": "następna",
    "The source repeats this number {n} times": "Źródło powtarza ten numer {n} razy",
    "{head} (a numbering error in the source). The other copies are below. The first one is returned.": (
        "{head} (błąd numeracji w źródle). Poniżej pozostałe kopie. Zwracana jest pierwsza z nich."
    ),
    "Sub-units": "Jednostki podrzędne",
    "Showing 60 of {total} units. The rest is in the document's table of contents.": (
        "Pokazano 60 z {total} jednostek. Resztę znajdziesz w spisie treści dokumentu."
    ),
    "Not found": "Nie znaleziono",
    "See the list of documents": "Zobacz listę dokumentów",
    "{doc} is not indexed in this database. {list_link} or add the PDF file on the Sync tab.": (
        "{doc} nie jest zindeksowany w tej bazie. {list_link} albo dodaj plik PDF w zakładce Synchronizacja."
    ),
    "The current edition of {doc} has no unit with that number. Numbers are never guessed, so nothing was "
    "forced to match.": (
        "W bieżącej edycji {doc} nie ma jednostki o takim numerze. Numery nie są zgadywane, więc nic nie "
        "zostało dopasowane na siłę."
    ),
    "Probably renumbered to": "Prawdopodobnie przenumerowane na",
    "Based on the report of the last sync (text similarity).": (
        "Na podstawie raportu z ostatniej synchronizacji (podobieństwo tekstu)."
    ),
    "Nearest existing numbers": "Najbliższe istniejące numery",
    # ---- verify quote
    "OK: matches the text and the PDF": "OK: zgodny z tekstem i PDF",
    "NO MATCH: does not match": "NO MATCH: brak zgodności",
    "The quote appears verbatim in this unit and in the text layer of the PDF.": (
        "Cytat występuje dosłownie w tej jednostce i w warstwie tekstowej PDF."
    ),
    "Do not use this quote without correcting it.": "Nie wolno używać tego cytatu bez poprawki.",
    "In unit {ref}: {answer}": "W jednostce {ref}: {answer}",
    "In the PDF: {answer}": "W PDF: {answer}",
    "In the PDF text layer: {answer}": "W warstwie tekstowej PDF: {answer}",
    "not checked": "nie sprawdzono",
    "The PDF file on disk differs from the indexed edition (SHA-256 checksum mismatch).": (
        "Plik PDF na dysku różni się od zindeksowanej edycji (niezgodna suma SHA-256)."
    ),
    "The quote appears in other units of this document:": "Cytat występuje w innych jednostkach tego dokumentu:",
    "Closest fragment in the document · similarity {pct}%": "Najbliższy fragment w dokumencie · podobieństwo {pct}%",
    "Paste a quote to verify.": "Wklej cytat do sprawdzenia.",
    "Verification failed: {error}": "Weryfikacja nie powiodła się: {error}",
    # ---- search
    "Three ways to reach the verbatim text and clause number: by number, full text and hybrid.": (
        "Trzy sposoby dotarcia do dosłownego tekstu i numeru punktu: po numerze, pełnotekstowo i hybrydowo."
    ),
    "Search type": "Rodzaj wyszukiwania",
    "Search form": "Formularz wyszukiwania",
    "Document number": "Numer dokumentu",
    "Clause, table or figure number": "Numer punktu, tabeli lub rysunku",
    "Find": "Znajdź",
    "Show sub-units": "Pokaż jednostki podrzędne",
    "Show neighboring numbers": "Pokaż sąsiednie numery",
    "Accepted prefixes: {prefixes}. Separate several numbers with a semicolon. Numbers are never guessed.": (
        "Dozwolone przedrostki: {prefixes}. Kilka numerów rozdziel średnikiem. Numery nie są zgadywane."
    ),
    "Full-text query": "Zapytanie pełnotekstowe",
    "Question or phrase (PL or EN)": "Pytanie lub fraza (PL albo EN)",
    "Document": "Dokument",
    "All documents": "Wszystkie dokumenty",
    "Unit kind": "Rodzaj jednostki",
    "Syntax": "Składnia",
    "Safe": "Bezpieczna",
    "FTS5 (advanced)": "FTS5 (zaawansowana)",
    "BM25 weight relative to vectors": "Waga BM25 względem wektorów",
    "vectors only": "tylko wektory",
    "BM25 only": "tylko BM25",
    "Vectors": "Wektory",
    "The weights add up to 1. On an English corpus with Polish questions a BM25 weight of about 0.3–0.4 works "
    "better.": ("Suma wag = 1. Na korpusie angielskim z pytaniami po polsku lepiej działa waga BM25 ok. 0,3–0,4."),
    "Include figures": "Uwzględnij rysunki",
    "Query syntax": "Składnia zapytań",
    "Words are joined with AND by default. A quoted phrase is taken literally. Special characters do not break "
    "the query.": (
        "Słowa łączone domyślnie spójnikiem AND. Frazę w cudzysłowie traktujemy dosłownie. Znaki specjalne nie "
        "psują zapytania."
    ),
    "exact phrase": "dokładna fraza",
    "prefix (cylinder, cylinders…)": "przedrostek (cylinder, cylinders…)",
    "logical operators (in capitals)": "operatory logiczne (wielkimi literami)",
    "words within 5 words of each other": "słowa w odległości do 5 wyrazów",
    "An invalid expression does not crash anything, it just shows the parser message. Numbers such as {number} "
    "are treated as a phrase.": (
        "Błędne wyrażenie nie powoduje awarii, tylko wyświetla komunikat parsera. Numery typu {number} są frazą."
    ),
    "Ask a question or enter a number": "Zadaj pytanie albo podaj numer",
    "Results appear here as evidence cards: with a citation to paste, the verbatim text and where the result "
    "came from.": (
        "Wyniki pojawią się tutaj jako karty dowodowe: z cytowaniem do wklejenia, dosłownym tekstem i informacją, "
        "skąd wziął się wynik."
    ),
    "Query:": "Zapytanie:",
    "The query is not valid.": "Zapytanie nie jest poprawne.",
    "Switch the syntax to “Safe” or fix the expression (see the “Query syntax” help).": (
        "Przełącz składnię na „Bezpieczna” albo popraw wyrażenie (patrz pomoc „Składnia zapytań”)."
    ),
    "Expression passed to FTS5": "Wyrażenie przekazane do FTS5",
    "BM25 only, no vectors or network": "tylko BM25, bez wektorów i sieci",
    "No hits": "Brak trafień",
    "Try a shorter phrase, a prefix with an asterisk (e.g. cylind*) or hybrid search, which understands "
    "questions in Polish.": (
        "Spróbuj krótszej frazy, przedrostka z gwiazdką (np. cylind*) albo wyszukiwania hybrydowego, które rozumie "
        "pytania po polsku."
    ),
    "BM25 {w_fts} / vectors {w_vec}": "BM25 {w_fts} / wektory {w_vec}",
    "reranker: {name}": "reranker: {name}",
    "No matching units found. Change the query or remove the document or unit-kind filter.": (
        "Nie znaleziono pasujących jednostek. Zmień zapytanie, usuń filtr dokumentu albo rodzaju jednostki."
    ),
    "exact number": "dokładny numer",
    "weighted RRF fusion (BM25 + vectors)": "ważona fuzja RRF (BM25 + wektory)",
    "vectors": "wektory",
    "Vector search is unavailable, results come from BM25 only.": (
        "Wyszukiwanie wektorowe jest niedostępne, wyniki pochodzą tylko z BM25."
    ),
    "Searching figures by image is unavailable.": "Wyszukiwanie rysunków po obrazie jest niedostępne.",
    "The reranker failed, the order from RRF fusion was kept.": (
        "Reranker nie zadziałał, zachowano kolejność z fuzji RRF."
    ),
    "A document named in the query is not indexed.": "Dokument wymieniony w zapytaniu nie jest zindeksowany.",
    "Enter a document number (e.g. 4-12) or type it together with the clause number.": (
        "Podaj numer dokumentu (np. 4-12) albo wpisz go razem z numerem punktu."
    ),
    "Search failed: {error}": "Wyszukiwanie nie powiodło się: {error}",
    # ---- sync page and jobs
    "Add new editions of the documents. Only new or changed files are processed, and the old edition of the "
    "same document is replaced.": (
        "Dodaj nowe edycje dokumentów. Przetwarzane są tylko pliki nowe lub zmienione, a stara edycja "
        "tego samego dokumentu jest zastępowana."
    ),
    "The jobs module is not available in this instance.": "Moduł zadań nie jest dostępny w tej instancji.",
    "Browsing and search work, but starting a sync from this page is disabled. You can run a sync from the "
    "command line: {command}": (
        "Przeglądanie i wyszukiwanie działają, ale uruchamianie synchronizacji z poziomu tej strony jest wyłączone. "
        "Synchronizację można uruchomić z wiersza poleceń: {command}"
    ),
    "Add PDF documents": "Dodaj dokumenty PDF",
    "PDF folder on the server: {path}": "Katalog PDF na serwerze: {path}",
    "Drag PDF files here or click to choose": "Przeciągnij pliki PDF tutaj albo kliknij, aby wybrać",
    "NUMBER_YYYYMM.pdf": "NR_RRRRMM.pdf",
    "Only {ext} files named {pattern} (e.g. {example}). You can pick several files at once.": (
        "Tylko pliki {ext} w nazwie {pattern} (np. {example}). Można wskazać wiele plików naraz."
    ),
    "Selected files": "Wybrane pliki",
    "Fetch newer editions from {source}": "Pobierz nowsze edycje z {source}",
    "Checks the catalog and downloads only documents that have a newer edition than the local one. Needs "
    "internet access.": (
        "Sprawdza katalog i pobiera tylko dokumenty, które mają nowszą edycję niż lokalna. Wymaga dostępu do internetu."
    ),
    "Start sync": "Uruchom synchronizację",
    "With no files and the option unchecked, only the PDF folder is checked.": (
        "Bez plików i bez zaznaczenia opcji sprawdzony zostanie sam katalog PDF."
    ),
    "Job history": "Historia zadań",
    "Catalog of {source}": "Katalog {source}",
    "The catalog has not been fetched yet, so it is not known which editions are out of date.": (
        "Katalog nie został jeszcze pobrany, więc nie wiadomo, które edycje są nieaktualne."
    ),
    "Refresh the catalog of {source}": "Odśwież katalog {source}",
    "Fetches the list of current documents and their editions. Does not change the local database.": (
        "Pobiera listę aktualnych dokumentów i ich edycji. Nie zmienia lokalnej bazy."
    ),
    "Change reports": "Raporty zmian",
    "No reports": "Brak raportów",
    "A report of the differences between editions is written after the first sync that replaces a document.": (
        "Raport z różnicami między edycjami powstaje po pierwszej synchronizacji, która zastąpi dokument."
    ),
    "Job {id} was not found or the jobs module is unavailable.": (
        "Nie znaleziono zadania {id} albo moduł zadań jest niedostępny."
    ),
    "created {when}": "utworzono {when}",
    "duration {value}": "czas {value}",
    "Job progress": "Postęp zadania",
    "Waiting in the queue…": "Oczekuje w kolejce…",
    "Working…": "Pracuję…",
    "The job ended with an error.": "Zadanie zakończyło się błędem.",
    "Job log": "Dziennik zadania",
    "Last log lines": "Ostatnie linie dziennika",
    "Cancellation requested.": "Anulowanie zgłoszone.",
    "The job is finishing its current step (for a sync: the document being processed right now), and the "
    "remaining documents will be skipped. Work already done is kept.": (
        "Zadanie kończy bieżący krok (przy synchronizacji: dokument, który jest właśnie przetwarzany), a pozostałe "
        "dokumenty zostaną pominięte. Wykonana już praca zostaje."
    ),
    "Cancel this job? Documents already processed are kept, the rest are skipped.": (
        "Anulować to zadanie? Dokumenty już przetworzone zostaną zachowane, pozostałe zostaną pominięte."
    ),
    "Cancelling…": "Anulowanie…",
    "Cancel": "Anuluj",
    "View the change report": "Zobacz raport zmian",
    "View results": "Zobacz wyniki",
    "Result (raw data)": "Wynik (dane surowe)",
    "Job": "Zadanie",
    "No jobs in the history": "Brak zadań w historii",
    "After you start a sync, a citation check or a catalog refresh, the job will appear here.": (
        "Po uruchomieniu synchronizacji, sprawdzania cytowań albo odświeżenia katalogu zadanie pojawi się tutaj."
    ),
    "Job history is unavailable": "Historia zadań jest niedostępna",
    "The jobs module is not loaded in this instance.": "Moduł zadań nie jest załadowany w tej instancji.",
    "Could not start the job: {error}": "Nie udało się uruchomić zadania: {error}",
    "No such job.": "Nie znaleziono takiego zadania.",
    "This job has already finished, there is nothing to cancel.": (
        "To zadanie już się zakończyło, nie ma czego anulować."
    ),
    "This job cannot be interrupted midway, it will finish by itself shortly.": (
        "Tego zadania nie można przerwać w trakcie, zakończy się samo po chwili."
    ),
    "Cannot cancel the job: {error}": "Nie można anulować zadania: {error}",
    # ---- change report
    "Report {name}": "Raport {name}",
    "Report not found": "Nie znaleziono raportu",
    "The report “{name}” does not exist in the reports folder.": ("Raport „{name}” nie istnieje w katalogu raportów."),
    "Report of changes between editions": "Raport zmian między edycjami",
    "What changed after documents were replaced: numbers that disappeared or appeared, changed text and probable "
    "renumberings.": (
        "Co się zmieniło po wymianie dokumentów: numery, które zniknęły lub się pojawiły, zmieniony tekst "
        "i prawdopodobne przenumerowania."
    ),
    "Generated:": "Wygenerowano:",
    "Summary": "Podsumowanie",
    "Added documents": "Dodane dokumenty",
    "Replaced editions": "Zastąpione edycje",
    "Removed (no PDF)": "Usunięte (brak PDF)",
    "Unchanged": "Bez zmian",
    "The report contains no document sections": "Raport nie zawiera sekcji dokumentów",
    "This file has no document headings, or its format is unknown.": (
        "Ten plik nie ma nagłówków dokumentów albo ma nieznany format."
    ),
    "Edition replaced": "Edycja zastąpiona",
    "New document": "Nowy dokument",
    "Removed": "Usunięty",
    "disappeared": "zniknęło",
    "new": "nowych",
    "with changed text": "ze zmienionym tekstem",
    "renumberings": "przenumerowań",
    "for manual review": "do ręcznej oceny",
    "unchanged": "bez zmian",
    "Same number, changed text": "Ten sam numer, zmieniony tekst",
    "removed": "usunięto",
    "added": "dodano",
    "(word-by-word comparison, unchanged fragments shortened to “…”)": (
        "(porównanie słowo po słowie, fragmenty bez zmian skrócono do „…”)"
    ),
    "Probable renumberings (old → new)": "Prawdopodobne przenumerowania (stary → nowy)",
    "Old number": "Stary numer",
    "New number": "Nowy numer",
    "Text similarity": "Podobieństwo tekstu",
    "Heading": "Nagłówek",
    "Weaker candidates (for manual review)": "Słabsze kandydatury (do ręcznej oceny)",
    "Numbers that disappeared (no text match)": "Numery, które zniknęły (bez dopasowania tekstu)",
    "None. Every disappeared number was matched to a new one.": (
        "Brak. Każdy zniknięty numer został dopasowany do nowego."
    ),
    "New numbers (no counterpart in the old edition)": "Nowe numery (bez odpowiednika w starej edycji)",
    "The document's own changes section (verbatim)": "Sekcja zmian z samego dokumentu (dosłownie)",
    "Document removed (no PDF file in the folder)": "Dokument usunięty (brak pliku PDF w katalogu)",
    "New document: {units} units, {chunks} chunks": "Nowy dokument: {units} jednostek, {chunks} fragmentów",
    # ---- citations page
    "Checks that every document reference in your notes (for example “{example}”) points to an existing unit in "
    "the current edition of the document.": (
        "Sprawdza, czy każde powołanie na dokument w Twoich notatkach (na przykład „{example}”) wskazuje istniejący "
        "punkt w bieżącej edycji dokumentu."
    ),
    "Checks that every document reference in your notes points to an existing unit in the current edition of the "
    "document.": (
        "Sprawdza, czy każde powołanie na dokument w Twoich notatkach wskazuje istniejący punkt w bieżącej edycji "
        "dokumentu."
    ),
    "Run a check": "Uruchom sprawdzenie",
    "Repository folder (optional)": "Katalog repozytorium (opcjonalnie)",
    "the default folder configured on the server": "domyślny katalog skonfigurowany na serwerze",
    "Check citations": "Sprawdź cytowania",
    "Files of type {md} and {txt} are searched. The result replaces the previous one when the job finishes.": (
        "Przeszukiwane są pliki {md} i {txt}. Wynik zastępuje poprzedni po zakończeniu zadania."
    ),
    "The jobs module is unavailable in this instance.": "Moduł zadań jest niedostępny w tej instancji.",
    "Latest results": "Ostatnie wyniki",
    "time unknown": "czas nieznany",
    "No check results": "Brak wyników sprawdzenia",
    "Run a check above. When the job finishes, a table appears here: file:line, citation, status and a "
    "suggested fix.": (
        "Uruchom sprawdzenie powyżej. Po zakończeniu zadania pojawi się tu tabela: plik:linia, cytowanie, status "
        "i sugestia poprawki."
    ),
    "No citations found": "Nie znaleziono żadnych cytowań",
    "The files checked contain no document references in a recognized format.": (
        "W sprawdzonych plikach nie ma powołań na dokumenty w rozpoznawanym formacie."
    ),
    "Status filter": "Filtr statusu",
    "Filter by text": "Filtruj tekstowo",
    "Filter by file, citation or suggestion": "Filtruj po pliku, cytowaniu, sugestii",
    "File:line": "Plik:linia",
    "Citation": "Cytowanie",
    "Suggestion": "Sugestia",
    "Copy the path {path}": "Kopiuj ścieżkę {path}",
    "Copy path": "Kopiuj ścieżkę",
    "No rows for this filter": "Brak wierszy dla tego filtra",
    "Change the status or the filter phrase.": "Zmień status albo frazę filtra.",
    # ---- MCP page and client snippets
    "How to connect an AI assistant (Claude Code, Cursor, VS Code, Claude Desktop) to this database through the "
    "Model Context Protocol. The page never shows the API key: the configurations refer to an environment "
    "variable or prompt for the key.": (
        "Jak podłączyć asystenta AI (Claude Code, Cursor, VS Code, Claude Desktop) do tej bazy przez Model Context "
        "Protocol. Strona nie pokazuje klucza API: konfiguracje odwołują się do zmiennej środowiskowej albo pytają "
        "o klucz."
    ),
    "MCP server is running": "Serwer MCP działa",
    "MCP server unavailable": "Serwer MCP niedostępny",
    "Address": "Adres",
    "Copy the MCP endpoint address": "Kopiuj adres endpointu MCP",
    "Server name": "Nazwa serwera",
    "Streamable HTTP, POST only, sessionless (each request is independent)": (
        "Streamable HTTP, tylko POST, bez sesji (każde żądanie niezależne)"
    ),
    "Protocol": "Protokół",
    "MCP {protocol}; older clients (the {handshake} handshake) work too": (
        "MCP {protocol}; starsze klienty (handshake {handshake}) też działają"
    ),
    "Service version": "Wersja usługi",
    "Authentication": "Uwierzytelnianie",
    "key": "klucz",
    "Header {header} or {bearer}; the key is the value of the server variable {env}.": (
        "Nagłówek {header} albo {bearer}; klucz to wartość zmiennej {env} serwera."
    ),
    "Key is set on the server": "Klucz ustawiony na serwerze",
    "Key is not set: MCP answers 503": "Klucz nieustawiony: MCP odpowiada 503",
    "Scope": "Zakres",
    "Read-only. Sync and administrative jobs are available only in this dashboard.": (
        "Tylko odczyt. Synchronizacja i zadania administracyjne są dostępne wyłącznie w tym panelu."
    ),
    "The MCP module is not mounted in this instance. Details are in the server log (the “MCP not mounted” entry).": (
        "Moduł MCP nie został zamontowany w tej instancji. Szczegóły są w logu serwera (wpis „MCP not mounted”)."
    ),
    "Client configuration": "Konfiguracja klientów",
    "Pick your client, copy the configuration and paste it into the place shown. Restart the client after "
    "changing the configuration.": (
        "Wybierz swojego klienta, skopiuj konfigurację i wklej ją we wskazane miejsce. Po zmianie konfiguracji "
        "uruchom klienta ponownie."
    ),
    "Copy the configuration": "Kopiuj konfigurację",
    "Copy the configuration: {title}": "Kopiuj konfigurację: {title}",
    "Configuration: {title}": "Konfiguracja: {title}",
    "Tools ({count})": "Narzędzia ({count})",
    "external API, daily budget": "zewnętrzne API, budżet dzienny",
    "local, no cost": "lokalne, bez kosztów",
    "No tool list": "Brak listy narzędzi",
    "The MCP server module is not available in this instance.": (
        "Moduł serwera MCP nie jest dostępny w tej instancji."
    ),
    "terminal (bash or PowerShell), outside a claude session": "terminal (bash lub PowerShell), poza sesją claude",
    "Claude Code through kprun (secrets manager)": "Claude Code przez kprun (menedżer sekretów)",
    "~/.cursor/mcp.json (global) or .cursor/mcp.json (project)": (
        "~/.cursor/mcp.json (globalnie) albo .cursor/mcp.json (projekt)"
    ),
    "terminal, outside a claude session, requires {kprun}": "terminal, poza sesją claude, wymaga {kprun}",
    "Claude Desktop (mcp-remote bridge)": "Claude Desktop (most mcp-remote)",
    ".vscode/mcp.json or the command “MCP: Open User Configuration”": (
        ".vscode/mcp.json albo polecenie „MCP: Open User Configuration”"
    ),
    "<paste the API key>": "<wklej klucz API>",
    # ---- errors raised by routes
    "An HTMX request is required.": "Wymagane żądanie HTMX.",
    "No such document": "Nie ma takiego dokumentu",
    "{doc} is not indexed in this database.": "{doc} nie jest zindeksowany w tej bazie.",
    "No image for this figure.": "Brak obrazu rysunku.",
    "No image for this page.": "Brak obrazu strony.",
    # ---- app.js (see JS_STRINGS)
    "Copied": "Skopiowano",
    "Copied to clipboard": "Skopiowano do schowka",
    "Could not copy. Select the text manually.": "Nie udało się skopiować. Zaznacz tekst ręcznie.",
    "Could not load the image.": "Nie udało się wczytać obrazu.",
    "Remove {name}": "Usuń {name}",
    "Selected {n} file(s), {size} in total.": "Wybrano {n} plik(ów), łącznie {size}.",
    "Skipped files that are not PDF: {names}": "Pominięto pliki inne niż PDF: {names}",
    "response code {status}": "kod odpowiedzi {status}",
    "the server returned no job identifier": "serwer nie zwrócił identyfikatora zadania",
    "Job started": "Zadanie uruchomione",
    "Could not start the sync: {error}": "Nie udało się uruchomić synchronizacji: {error}",
    "Server error ({code}). Try again.": "Błąd serwera ({code}). Spróbuj ponownie.",
    "Server error. Try again.": "Błąd serwera. Spróbuj ponownie.",
    "No connection to the server.": "Brak połączenia z serwerem.",
}

#: English singular (or the explicit ``key=``) -> Polish forms for 1, 2-4 and 5+ items
PL_PLURAL: dict[str, tuple[str, str, str]] = {
    "candidate": ("kandydat", "kandydaci", "kandydatów"),
    "citation": ("cytowanie", "cytowania", "cytowań"),
    "document": ("dokument", "dokumenty", "dokumentów"),
    "of-document": ("dokumentu", "dokumentów", "dokumentów"),
    "new": ("nowy", "nowe", "nowych"),
    "replaced": ("zastąpiony", "zastąpione", "zastąpionych"),
    "removed": ("usunięty", "usunięte", "usuniętych"),
    "unit matches the query": (
        "jednostka pasuje do zapytania",
        "jednostki pasują do zapytania",
        "jednostek pasuje do zapytania",
    ),
}

#: Texts ``app.js`` passes to ``T()``; their translations are embedded in every page (the English page gets none)
JS_STRINGS: tuple[str, ...] = (
    "Collapse",
    "Copied",
    "Copied to clipboard",
    "Could not copy. Select the text manually.",
    "Could not load the image.",
    "Could not start the sync: {error}",
    "Dark theme",
    "Job started",
    "Light theme",
    "No connection to the server.",
    "PDF page",
    "Remove {name}",
    "Selected {n} file(s), {size} in total.",
    "Server error ({code}). Try again.",
    "Server error. Try again.",
    "Show all",
    "Skipped files that are not PDF: {names}",
    "response code {status}",
    "the server returned no job identifier",
)
