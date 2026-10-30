# BreakoutSignals2026 — BREAKOUT, FAST a FAST MICRO

Repozitár od 28.10.2026 obsahuje iba tri vybrané skenery.

| Skener | Program | GitHub Actions | Režim |
| --- | --- | --- | --- |
| Klasický BREAKOUT V2.5.2 | `scanner_v25_2.py` | `.github/workflows/scanner.yml` | Alpaca PAPER, 10 US akcií, pevný SL a TP 1:2 |
| FAST V3.3.1 | `scanner_fast.py` | `.github/workflows/fast_scanner.yml` | Alpaca PAPER, 28 US akcií, trailing stop 1,2 % |
| FAST MICRO V1.0 | `scanner_fast_micro_v1.py` + `fast_micro_v1.py` | `.github/workflows/fast_micro_scanner.yml` | Iba virtuálna simulácia, bez objednávok |

## Fungovanie

- Klasický BREAKOUT používa päťminútové dáta Twelve Data. Odhadované riziko je 5 USD na obchod.
- FAST používa päťminútové IEX dáta. Odhadované riziko je 3 USD na obchod, najviac 8 nových vstupov denne, 2 súčasné sledované pozície a 1 nový vstup do symbolu denne. Pri zaznamenanom realizovanom dennom PnL plus zápornom otvorenom PnL −12 USD alebo menej blokuje nové vstupy.
- FAST MICRO používa technické signály FAST, ale neodosiela objednávky. Modeluje riziko 1 USD, stop 0,50 %, TP 2,5R a sklz 0,05 % na každej strane plus odhad spreadu. Limity: 12 pokusov denne, 2 súčasné simulované obchody, 1 pokus na symbol/deň, blokovanie nových pokusov pri realizovanej dennej simulovanej strate 6 USD.
- `equity_candle_guard.py` je spoločná kontrola uzavretých a súvislých päťminútových sviečok. `scripts/fast_micro_session.sh` spúšťa a ukladá FAST MICRO.
- Samostatný FAST virtuálny tracker bol odstránený. FAST analyzuje pôvodné technické signály priamo; FAST MICRO má potrebné funkcie na spracovanie sviečok vo vlastnom module.

PAPER workflow používajú výhradne `https://paper-api.alpaca.markets`. Rizikové rozpočty nie sú garantovaným stropom straty. Virtuálne výsledky FAST MICRO nie sú skutočné plnenia Alpaca PAPER.

## Automatické spustenie

Od 30.10.2026 workflow neobsahujú úlohu `preflight` ani závislosti od jej výstupov. Plánovaný alebo manuálny beh ide priamo do `first-session`; po nej nasleduje `second-session`. Druhá úloha môže pokračovať po chybe prvej, ale rešpektuje zrušenie celého behu.

Zostáva pôvodný denný pár cronov **13:10 a 14:10 UTC**, pondelok až piatok. Pri letnom čase USA ide o 09:10 a 10:10 New York, pri zimnom o 08:10 a 09:10. Nie je tu ďalšia kontrola, ktorá by beh odmietla podľa zápisu cronu alebo sezóny. Čas otvorenia trhu, sviatky, existujúce hranice vstupov a EOD ochrany kontrolujú pôvodné slučky a samotné programy.

Na Slovensku sú UTC pokusy počas slovenského zimného času o **14:10 a 15:10**, počas letného o **15:10 a 16:10**. Po prijatom dennom štarte pokračujú vnútorné skenovacie slučky; nejde o nové workflow každých päť minút.

Odovzdanie prvej a druhej relácie je najneskôr o 12:00 NY (pri skrátenej seanse môže byť klasický BREAKOUT odovzdaný skôr). Prvá FAST a MICRO úloha má limit 300 minút, aby zvládla aj zimný skorý UTC štart o 08:10 NY. Žiadna úloha neprekračuje šesťhodinový limit runnera.

Samostatné concurrency skupiny povoľujú najviac jeden súčasný beh toho istého skenera; záložný pokus neruší aktívny beh. BREAKOUT má zachované EOD pokusy o 19:45 a 20:45 UTC. Samostatný watchdog zostáva odstránený.

GitHub môže vytvorenie plánovaného behu oneskoriť alebo vynechať. Odstránenie `preflight` odstráni jednu možnosť preskočenia skenovacích úloh, ale samo osebe nedokazuje opravu doručovania udalostí `schedule`. Funkčnosť treba overiť na skutočnom automatickom behu.

PAPER skenery zachovávajú monitorovanie, ochranu sledovaných pozícií a pokus o ich uzatvorenie približne 15 minút pred skutočným koncom seansy podľa kalendára Alpaca. Výpadok runnera alebo API môže zabrániť uzatvoreniu.

Pri manuálnom spustení FAST a BREAKOUT je predvolene vypnuté otváranie nových obchodov. Voľba `one_paper_trade` povoľuje jednorazovú skúšku; `full_paper_session` povoľuje celú PAPER seansu s existujúcimi ochranami. FAST MICRO zostáva iba virtuálny.

## Zachované stavy a výsledky

| Skener | Súbory |
| --- | --- |
| BREAKOUT | `breakout_state_v252.json`, `breakout_results_v252.csv` |
| FAST | `breakout_state_fast_v31.json`, `breakout_trades_fast_v31.jsonl` |
| FAST MICRO | `fast_micro_state_v1.json`, `fast_micro_signals_v1.jsonl`, `fast_micro_events_v1.jsonl`, `fast_micro_results_v1.csv` |

Tieto súbory sa aktualizujú automaticky. História a stav ponechaných skenerov zostali zachované; názov `v31` pri FAST je historický a naďalej používaný. Stavové súbory nemažte počas prevádzky.

## Nastavenie

Python 3.12 a balík `requests`; workflow ho inštalujú automaticky.

GitHub Actions Secrets: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `TELEGRAM_TOKEN`, `CHAT_ID` a pre klasický BREAKOUT aj `TWELVE_API_KEY`. Hodnoty kľúčov nepatria do kódu.

## Záloha pred čistením

Pred zmenou bola vytvorená vetva `backup/2026-10-28-before-keep-breakout-fast-micro`. Obsahuje pôvodný kód a commitnuté dáta. História GitHub Actions, Secrets a pozície či objednávky v Alpaca nie sú súčasťou tejto gitovej zálohy. Čistenie repozitára samo osebe neuzatvára broker pozície.
