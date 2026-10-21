# Záloha a obnova — BreakoutSignals2026/breakout-scanner

Dátum upratania: **21. október 2026**.

## Dve zálohy v GitHube

1. **Úplný stav pred čistením:** vetva
   [backup/2026-10-21-before-cleanup](https://github.com/BreakoutSignals2026/breakout-scanner/tree/backup/2026-10-21-before-cleanup).
   Obsahuje aj staré verzie Python programov, testovací AAPL workflow,
   staré stavové súbory a historické výsledky. Pevný východiskový commit:
   `cbcfda1da205cc3c616ac0a92b4e8ff2d4a22a84`.
2. **Čisté produkčné PAPER nastavenie:**
   [backup/2026-10-21-clean-paper-config](https://github.com/BreakoutSignals2026/breakout-scanner/tree/backup/2026-10-21-clean-paper-config).
   Dva aktívne scannery, ich workflow, bezpečne zachované výsledky,
   nastavenia a táto dokumentácia.

Každá vetva je samostatná kópia kompletného stavu súborov v danom okamihu.
GitHub vie zobraziť aj stiahnuť ZIP z konkrétnej vetvy.

## Čo sa vyčistilo z hlavnej vetvy

- `.github/workflows/main.yml` — starý samostatný manuálny test AAPL.
- `alpaca_executor.py` — nepoužívaný samostatný executor.
- `scanner.py`, `scanner_v25.py`, `scanner_v25_1.py` — predchádzajúce
  verzie; ich kódy ostávajú v zálohe *pred čistením*.
- `breakout_state_v24.txt` a prázdny `breakout_trades_v25.txt` —
  historické/nepoužívané súbory, dostupné v úplnej zálohe.
- `breakout_results_v25.csv` — presunutý bez zmeny obsahu do
  `archive/breakout_results_v25.csv`.

## Obnova (bezpečný postup)

1. Najprv vypni automatické GitHub Actions vstupy a skontroluj otvorené
   pozície, čakajúce objednávky a trailing/bracket stop v Alpaca PAPER.
2. Otvor požadovanú záložnú vetvu GitHubu a porovnaj ju s `main`.
   Kód obnov z nej; **neprepisuj naslepo živé stavové súbory**, pretože
   môžu odvtedy obsahovať novšie vykonané obchody.
3. Skontroluj, že workflow volajú `scanner_fast.py` a
   `scanner_v25_2.py` a že všetky API adresy ostali PAPER.
4. V **Settings → Secrets and variables → Actions** obnov potrebné
   kľúče (hodnoty Secrets GitHub nedovolí zobraziť ani exportovať).
5. Over syntax, testy a PAPER pozície. Až potom znovu povoľ
   automatické obchodovanie. Pri otvorených pozíciách môže byť potrebné
   manuálne zosúladenie stavu; nevymazávaj históriu objednávok.

## Obmedzenia zálohy

Zálohy GitHubu **neobsahujú** hodnoty Secrets, prístupové kľúče,
Telegram históriu, externé dáta Twelve Data, Alpaca účet alebo históriu
GitHub Actions behov. Obsahujú len súbory, ktoré boli commitnuté v GitHube.
Hodnoty API kľúčov do žiadnej zálohy nekopíruj.
