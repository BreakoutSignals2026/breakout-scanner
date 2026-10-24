# BreakoutSignals2026 — Alpaca PAPER scannery

Repozitár obsahuje **dva aktívne PAPER obchodné scannery** a **tretí ORB
V1.0 iba na simuláciu**. FAST a BREAKOUT obchodujú cez
`https://paper-api.alpaca.markets`; ORB neodosiela do Alpaca žiadne objednávky.
Nie je tu žiadny LIVE trading workflow.

## Aktívne programy

| Scanner | Python | GitHub Actions | Stratégia |
| --- | --- | --- | --- |
| FAST V3.3.1 | `scanner_fast.py` | `.github/workflows/fast_scanner.yml` | 28 akcií; breakout, trailing stop 1,2 % |
| BREAKOUT V2.5.2 | `scanner_v25_2.py` | `.github/workflows/scanner.yml` | 10 akcií; breakout, pevný stop + TP 1:2 |
| ORB V1.0 – simulácia | `scanner_orb_v1.py` | `.github/workflows/orb_scanner.yml` | 28 akcií; prvých 15 minút, hypotetický SL + TP 1:2; **bez objednávok** |

- **FAST V3.3.1 PAPER:** plánované riziko **3 USD na obchod** (nie garantované maximum);
  najviac **8 nových FAST vstupov denne**, **2 súčasné FAST/sledované pozície**
  a **1 nový vstup do toho istého symbolu denne**.
- FAST zároveň **nezávisle virtuálne zaznamenáva všetky technické breakout
  signály** aj vtedy, keď je limit PAPER obchodov vyčerpaný; virtuálny
  simulátor vytvorí najviac jeden modelový obchod na symbol a deň a používa
  nasledujúcu uzavretú IEX sviečku (0,05 % sklz na každej strane), trailing
  stop 1,2 % a limit simulovaného rizika 3 USD. Hypotetické PnL **nie je
  PAPER skutočné plnenie**; nezahŕňa reálny spread, borrow poplatky ani
  skutočnú postupnosť cien v päťminútovke.
- FAST zablokuje nové vstupy, keď zaznamenaný realizovaný denný PnL plus
  záporný otvorený PnL dosiahne **−12 USD alebo menej**. Nejde o automatické
  uzatvorenie pozícií ani o zaručenú maximálnu stratu.
- Limity počíta z obchodného žurnálu aj histórie objednávok Alpaca PAPER,
  takže prežijú reštart GitHub workflow. Pri chýbajúcich/nesprávnych údajoch
  nové vstupy blokuje; trailing ochrana a EOD zostávajú aktívne.
- Klasický BREAKOUT má stále odhadované riziko **5 USD na obchod**;
  ORB zostáva iba virtuálny.
- Oba scannery kontrolujú otvorené objednávky a existujúce pozície.
  Nemanipulujte ručne s ich spravovanými pozíciami bez kontroly objednávok.
- Pred koncom obchodného dňa sa programy snažia ukončiť svoje pozície
  približne 15 minút pred skutočným koncom seansy podľa kalendára Alpaca.
  Pri výpadku GitHub Actions alebo Alpaca to **nie je záruka** uzatvorenia.

## Automatické spustenie

GitHub Actions používa štyri UTC crony (`13:20`, `14:20`, `13:30`,
`14:30`) počas pracovných dní kvôli zmenám letného času a obnove po
vynechanom spustení. Hlavný štart je **09:20 New York** (10 minút pred otvorením US burzy), záložný **09:30 NY** (otvorenie burzy).
`preflight` kontroluje čas a skupina `concurrency` bráni paralelným behov
toho istého scanneru. GitHub môže plánované spustenia oneskoriť alebo vynechať.

- **Plánovaný beh:** FAST a BREAKOUT môžu posielať PAPER vstupy; ORB iba simuluje.
- **Manuálny beh bez voľby `one_paper_trade`:** SAFE MODE – iba
  monitorovanie/večerná kontrola, bez nových vstupov.
- **Manuálny beh s `one_paper_trade=true`:** jednorazový vstupný test.
  Ak už bola jednorazová skúška použitá, scanner ďalší vstup zablokuje.
- **Manuálny beh s `full_paper_session=true`:** núdzové spustenie celej
  PAPER seansy pri vynechanom GitHub `schedule`; naďalej so všetkými dennými FAST limitmi a ochrannými pravidlami.
- Workflow používajú samostatné `concurrency` skupiny, aby sa nekryli dva
  behy **toho istého** scanneru.
- Pri odovzdávaní prvej a druhej časti seansy používajú oba obchodné scannery
  `actions/checkout` s `ref: main`, aby si načítali **najnovší commitnutý stav**.
  Starý snapshot zo začiatku workflow môže mylne označiť vlastné pozície ako cudzie.
- FAST V3.3.1 hlási široký IEX spread či starú cenu ako zamietnutý signál
  **pred odoslaním objednávky**, nie ako chybné vykonanie obchodu.
- ORB začne analyzovať až po vytvorení 15-minútového opening range, teda
  od 09:45 NY. Podrobnosti: [ORB_STRATEGY.md](ORB_STRATEGY.md).

## Dôležité súbory, ktoré sa nesmú len tak vymazať

| Súbor | Význam |
| --- | --- |
| `breakout_state_fast_v31.json` | Stav a história vstupov FAST; názov v31 je historický, aktuálna aplikácia ho naďalej používa |
| `breakout_trades_fast_v31.jsonl` | Skutočné zaznamenané FAST PAPER obchody a udalosti |
| `fast_virtual_state_v1.json` | Stav nezávislého FAST virtuálneho testu |
| `fast_virtual_signals_v1.jsonl` | Všetky rozpoznané FAST technické signály, bez broker objednávok |
| `fast_virtual_results_v1.csv` | Oddelené hypotetické FAST výsledky, nie Alpaca PAPER PnL |
| `breakout_state_v252.json` | Stav BREAKOUT V2.5.2; vytvorí sa pri potrebe |
| `breakout_results_v252.csv` | Overené výsledky BREAKOUT V2.5.2; vytvorí sa po uzatvorených obchodoch |
| `archive/breakout_results_v25.csv` | Historické výsledky staršieho scanneru; **nemiešať** s reálnymi PnL Alpaca |
| `orb_state_v1.json` | Aktuálny stav virtuálneho ORB obchodovania, **nie** stav Alpaca pozícií |
| `orb_results_v1.csv` | Hypotetické ORB výsledky, nesčítavať s reálnym PAPER PnL |

Súbory so stavom vznikajú alebo sa aktualizujú automaticky počas skenovania.
Ich odstránenie či prepísanie môže narušiť overovanie otvorených pozícií.

## GitHub Actions Secrets — iba názvy, NIE hodnoty

V repozitári treba mať cez **Settings → Secrets and variables → Actions**
nastavené `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `TELEGRAM_TOKEN`,
`CHAT_ID` a pre klasický BREAKOUT tiež `TWELVE_API_KEY`.
Kľúče patria do GitHub Secrets, **nikdy do kódu ani do zálohy ZIP**.

## Zálohovanie a obnova

Pozri [BACKUP_RESTORE.md](BACKUP_RESTORE.md). Pred čistením bola urobená úplná
gitová záloha na vetve `backup/2026-10-21-before-cleanup`. Vyčistené
nastavenie je na vetve `backup/2026-10-21-clean-paper-config`.

**Pozor:** GitHub vetvy/záložný ZIP chránia kód a commitnuté výsledky,
ale **nezálohujú** hodnoty Secrets, históriu Actions behu ani objednávky
a stav účtu uložené v Alpaca. Pri obnove treba overiť aktuálne pozície
a objednávky v Alpaca PAPER pred povolením obchodovania.
