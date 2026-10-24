# BreakoutSignals2026 — Alpaca PAPER scannery

Repozitár obsahuje **dva aktívne PAPER scannery US akcií**, FAST virtuálne signály,
**ORB V1.0 iba na simuláciu**, nový **CRYPTO scanner (24/7, oddelený Alpaca PAPER režim)**
a **európsky XETRA scanner (iba virtuálny)**. FAST a BREAKOUT obchodujú cez
`https://paper-api.alpaca.markets`; ORB neodosiela do Alpaca žiadne objednávky.
Nie je tu žiadny LIVE trading workflow.

## Aktívne programy

| Scanner | Python | GitHub Actions | Stratégia |
| --- | --- | --- | --- |
| FAST V3.3.1 | `scanner_fast.py` | `.github/workflows/fast_scanner.yml` | 28 akcií; breakout, trailing stop 1,2 % |
| BREAKOUT V2.5.2 | `scanner_v25_2.py` | `.github/workflows/scanner.yml` | 10 akcií; breakout, pevný stop + TP 1:2 |
| ORB V1.0 – simulácia | `scanner_orb_v1.py` | `.github/workflows/orb_scanner.yml` | 28 akcií; prvých 15 minút, hypotetický SL + TP 1:2; **bez objednávok** |
| CRYPTO V1.0 | `scanner_crypto_v1.py` + `crypto_paper_v1.py` | `.github/workflows/crypto_scanner.yml` | BTC/USD ETH/USD SOL/USD 24/7; nezávislé virtuálne obchody + oddelený Alpaca PAPER modul |
| XETRA V1.0 | `scanner_xetra_v1.py` | `.github/workflows/xetra_scanner.yml` | SAP.DE, SIE.DE, BMW.DE, ALV.DE, BAS.DE, DTE.DE; nemecké akcie, **iba virtuálne** |

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

## Nové trhy – spôsob fungovania a obmedzenia

### CRYPTO V1.0 (24 hodín denne, 7 dní v týždni)
- Dáta: Alpaca Market Data `/v1beta3/crypto/us/bars`, plne uzatvorené 5-minútové sviečky.
- Počiatočné technické parametre: 12 sviečok, breakout >=0,15 %, RelVol >=1,5.
- **Virtuálny backtest v reálnom čase**: hypotetický nasledujúci otvorený 5-min
  bar, SL 1,2 %, TP 2,4 %, odhad poplatkov 0,25 % za stranu a sklzu 0,08 %.
  Výsledky oddelené: `crypto_virtual_v1_state.json`,
  `crypto_virtual_v1_signals.jsonl`, `crypto_virtual_v1_trades.csv`.
- **Alpaca PAPER režim**: pred ďalším novým vstupom overuje cudzie krypto
  pozície/objednávky a stav svojej ochrannej SELL STOP-LIMIT/GTC. Max 3
  nové vstupy/UTC deň, 1 otvorená kryptopozícia, odhadované riziko 3 USD.
  Všetko len PAPER endpoint; cez víkend bez výnimky. Tento STOP-LIMIT
  **nemá zaručené vykonanie** (pri preskoku limitnej ceny ostane pozícia).
  To NIE JE trailing stop; nemá ochranu obchodovania vo výpadku GitHub.
- **Plánované behy majú CRYPTO PAPER nové vstupy povolené**, ale len
  cez pevný endpoint `paper-api.alpaca.markets` a po overení limitov, dát,
  vlastných objednávok a ochrannej stop-limit logiky. Pred prvou novou objednávkou
  musí byť broker stav jednoznačný. Ručný `Run workflow` je v SAFE MODE,
  pokiaľ nezvolíš `enable_paper_entries`. Monitorovanie existujúcich
  obchodov a ochrany beží bez ohľadu na režim.
  **Stop-limit pri prudkom pohybe nemusí uzavrieť pozíciu**; sleduj Telegram
  varovania. Softvérový ziskový výstup 2,4 % zruší stop najprv a pred market
  výstupom vyžaduje potvrdenie zrušenia od Alpaca.
  Výsledky: `crypto_paper_state_v1.json`, `crypto_paper_events_v1.jsonl`.
- Spot kryptomeny v Alpaca sú len LONG, bez SHORT; brakety a serverový
  trailing stop pre crypto nie sú podporované. Broker si účtuje crypto poplatky.
  Príkladom je uzatvorenie ochranného príkazu stop-limit, nie garantovaný SL.

### XETRA V1.0 (pracovné dni približne 09:00–17:30 Berlín)
- Dáta: `query1.finance.yahoo.com/v8/finance/chart/SAP.DE`, 5-minútové
  sviečky, len pri overení EUR a burzy XETRA/GER. Ide o **neoficiálny,
  potenciálne oneskorený** zdroj, ktorý môže v GitHub Actions vracať HTTP 429;
  pri neplatných alebo nedostupných dátach sa obchod nenasimuluje.
- Len virtuálne vyhodnocovanie (žiadne Alpaca objednávky). Na skutočný
  európsky PAPER účet cez Xetra potrebujeme iného brokera a legitímny zdroj
  intradenných dát. Odhadovaný SL 1 %, TP 2 %, riziko 3 EUR.
- Oddelené súbory `xetra_virtual_v1_state.json`,
  `xetra_virtual_v1_signals.jsonl`, `xetra_virtual_v1_trades.csv`.
- Virtuálny skener nepovažuje údaje Yahoo za uskutočnené obchody na Xetre.

**Čo je aktívne:** 24/7 CRYPTO virtuálny scanner aj oddelený Alpaca CRYPTO
PAPER režim s obmedzenými novými vstupmi + XETRA virtuálny scanner v hodinách
burzy. Automatické úspešné vstupy a výstupy závisia od dostupnosti API/dát;
neprepisujú sa doterajšie US akciové scannery.
