# Nezávislé štarty a MICRO PAPER

## Stav nasadenia

Kód a konfigurácia v repozitári sú pripravené. **Externý časovač nie je aktívny, kým nevytvoríte účet/úlohy podľa tohto návodu. MICRO PAPER neotvára nové obchody bez samostatných kľúčov.** Úspešná odpoveď na dispatch ešte nie je dôkaz spustenia runnera alebo obchodu.

## 1. Externý časovač: cron-job.org

Vytvorte účet na https://cron-job.org/en/. Tri HTTP úlohy volajú GitHub `workflow_dispatch`, ktorý pri manuálnom spustení fungoval. Obchádzame vytváranie udalostí GitHub `schedule`; programy a výsledky zostávajú na GitHube.

Každá úloha má časové pásmo **America/New_York**, pracovné dni pondelok–piatok, čas **09:20**, zálohy **09:25 a 09:30**. Súbežnosť workflow zostáva najviac jeden beh každého skenera a `cancel-in-progress: false`; zálohy nerušia aktívny beh. Alpaca kalendár kontroluje sviatky. O 09:20 sa štartujú programy, **nové vstupy sú až od 09:30 NY**. Toto riešenie negarantuje čas pridelenia GitHub runnera ani bezvýpadkovosť oboch služieb.

Na Slovensku: 30.10.2026 hlavný štart **14:20**, od 2.11.2026 **15:20**. IANA časové pásmo automaticky sleduje americké zmeny času.

### Prístupový token

Na GitHube vytvorte **fine-grained personal access token** iba pre `BreakoutSignals2026/breakout-scanner`, s oprávnením **Actions: Read and write**. Ostatné práva nepridávajte. Zvoľte primeranú platnosť a pred expiráciou ho obnovte. Token patrí do súkromnej konfigurácie časovača; **neposielajte ho do chatu ani neukladajte do repozitára**. Token umožňuje tretej službe spúšťať tieto workflow vrátane PAPER seansy.

### Tri úlohy cez webovú konzolu

Metóda **POST**, URL podľa tabuľky, spoločné telo a hlavičky nižšie:

| Skener | URL |
| --- | --- |
| BREAKOUT | `https://api.github.com/repos/BreakoutSignals2026/breakout-scanner/actions/workflows/scanner.yml/dispatches` |
| FAST | `https://api.github.com/repos/BreakoutSignals2026/breakout-scanner/actions/workflows/fast_scanner.yml/dispatches` |
| MICRO | `https://api.github.com/repos/BreakoutSignals2026/breakout-scanner/actions/workflows/fast_micro_scanner.yml/dispatches` |

Hlavičky:

```text
Authorization: Bearer VAS_GITHUB_TOKEN
Accept: application/vnd.github+json
Content-Type: application/json
```

Telo všetkých troch úloh:

```json
{"ref":"main"}
```

Workflow neprijímajú žiadne vstupné políčka. Ručný aj externý štart povoľujú bežnú PAPER seansu. Nezapínajte ukladanie odpovedí, pretože konfigurácia obsahuje citlivý token. Pri skúške overte v GitHub Actions nový beh aj spustenie skenovacej úlohy; počas otvoreného trhu môže skúška vytvoriť PAPER objednávky.

### Voliteľne automatické vytvorenie troch úloh

V cron-job.org → Settings vytvorte jeho API kľúč. Lokálne spustite:

```bash
python scripts/configure_external_starts.py
python scripts/configure_external_starts.py --apply
```

Prvý príkaz iba vypíše konfiguráciu s maskovaným tokenom. Druhý si tokeny vyžiada skryto a vytvorí/aktualizuje tri úlohy. Už existujúce úlohy identifikuje podľa presného názvu. Kľúče nevypisuje. Nedávajte tento konfiguračný skript do verejného GitHub Actions behu.

Po aktivácii skontrolujte v časovači čas vykonania, HTTP výsledok a v GitHube nový `workflow_dispatch` beh. Ak GitHub prijme požiadavku, ale skener nezačne, problém je v ďalšej fáze (fronta runnera, súbežnosť alebo chyba programu). Samotná HTTP odpoveď nepreukazuje vykonaný obchod.

## 2. Oddelený MICRO PAPER účet

V Alpaca PAPER dashboarde rozbaľte číslo účtu vľavo hore → **Open New Paper Account**. Pôvodný účet nemažte ani neresetujte. Pre nový účet vytvorte jeho API kľúče.

Do GitHub repozitára → Settings → Secrets and variables → Actions pridajte:

- `MICRO_ALPACA_API_KEY`
- `MICRO_ALPACA_SECRET_KEY`

Pôvodné `ALPACA_API_KEY` a `ALPACA_SECRET_KEY` ostávajú pre FAST/BREAKOUT a čítanie IEX dát. MICRO pred prvou zmenou objednávok porovná identity účtov a **odmietne rovnaký účet ako FAST**. Tým sa nebudú miešať pozície rovnakých akcií a porovnanie zostane zmysluplné.

MICRO používa odhadované riziko **1 USD**, pevný SL **0,50 %**, TP **2,5R**, najviac **12 pokusov denne**, **2 súčasné MICRO pozície**, jeden pokus na symbol/deň a dennú stratovú ochranu **6 USD** (realizovaný PnL plus záporný otvorený PnL). Riziko nie je garantovaným stropom; trhový sklz a gapy ho môžu prekročiť. Objednávky sú celé akcie s brokerovým bracket SL/TP, nie trailing stop. Na konci seansy sa obsluhujú iba preukázateľne vlastnené MICRO pozície, približne 15 minút pred skutočným close.

Bez nových kľúčov pokračuje virtuálny zber a v logu je `MICRO PAPER NOT CONFIGURED`. Ručné, plánované aj externé štarty povoľujú nové MICRO PAPER vstupy, ak sú nakonfigurované samostatné kľúče.

## Oddelenie výsledkov

- Pôvodné virtuálne súbory `fast_micro_*_v1.*` zostávajú nezmenené a nie sú vydávané za broker plnenia.
- `fast_micro_paper_state_v1.json` obsahuje PAPER objednávky, účet a stav ochrany.
- `fast_micro_paper_results_v1.csv` obsahuje iba overené skutočné Alpaca PAPER fill ceny a vypočítaný PnL pred prípadnými ďalšími poplatkami.
- Identifikátory MICRO začínajú `MICRO1_`; nesmú sa zamieňať s FAST31 alebo B252.

FAST už nemá limit dvoch súčasných pozícií. Nemá ani celkový denný limit vstupov, ani dennú stratovú brzdu. Zostáva jeden vstup na symbol/deň (ochrana duplicitných vstupov), odhadované riziko **3 USD** a trailing stop **1,2 %**. Limity brokera a buying power naďalej platia.

## Zdroje

- https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
- https://docs.cron-job.org/rest-api.html
- https://cron-job.org/en/faq/
- https://docs.alpaca.markets/us/docs/paper-trading
- https://docs.alpaca.markets/us/docs/orders-at-alpaca
