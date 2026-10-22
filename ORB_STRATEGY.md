# ORB V1.0 – 15-minútový opening range (iba virtuálne obchody)

## Čo robí

- **Nemení FAST ani BREAKOUT a NEPOSIELA ŽIADNE ALPACA OBJEDNÁVKY.**
- Z akcií FAST sleduje 28 symbolov na **Alpaca IEX 5-minútových sviečkach**.
- Pre každú akciu potrebuje všetky tri riadne uzavreté sviečky s časmi
  **09:30, 09:35 a 09:40 New York**. Chýbajúca IEX sviečka = bez obchodu.
- ORB maximum a minimum vznikne z týchto troch sviečok (09:30–09:45).
- Signál vzniká až po uzavretí ďalšej 5-minútovej sviečky, ktorá *uzavrie*
  nad/pod hranicou ORB a predchádzajúca sviečka ešte bola vnútri rozpätia.
- Vyžaduje prekročenie o aspoň **0,15 %**, relatívny objem aspoň **1,10×**
  oproti priemeru prvých troch sviečok a hodnotu IEX obchodu v signálovej
  sviečke aspoň **15 000 USD**. Ostatné filtre: cena **5–50 USD**,
  ORB šírka 0,25–5 %, signál najneskôr do **11:30 NY**.
- Maximálne jeden virtuálny obchod na ten istý symbol za deň. Nie je to
  spoločný limit počtu vstupov pre všetky akcie.

## Simulované plnenie a výstup

- Cena virtuálneho vstupu je **otvorenie nasledujúcej 5-minútovej sviečky**,
  upravené o predpokladaný sklz **0,05 %**. Plnenie potvrdíme až po uzavretí
  tejto sviečky, keď poznáme celú OHLC. **Nejde o skutočnú live kotáciu.**
- Stop: pri LONG 25 % šírky opening range pod ORB maximom; pri SHORT 25 %
  nad ORB minimom. Ak je stop vzdialený viac než 2 % od vstupu, signál sa
  vynechá. Profit target **2× vzdialenosť stopu**.
- Modelovaný rozpočet rizika **do 5 USD** pred neočakávanými gapmi a
  komisiami; virtuálna nominálna pozícia max. **1 000 USD**.
- SL a TP vyhodnocujeme podľa *celých uzavretých* päťminútových sviečok.
  Keď tá istá sviečka zasiahne oboje, **započíta sa SL ako prvý**.
  Pri cenovom gape proti pozícii sa simulovaná cena výstupu zhorší.
- Výstup sa simuluje najneskôr približne 15 minút pred skutočným koncom
  US seansy podľa kalendára Alpaca. Pri zastaraných dátach sa hypotetický
  výstup **nevymýšľa**.
- Náklady: predpokladaný sklz 0,05 % na každej strane. **Poplatky,
  úroky za požičanie pri shorte, skutočné spready a trhový vplyv nie sú
  započítané.** Výsledok je hypotetický hrubý PnL, nie Alpaca PAPER PnL.

## GitHub a dáta

| Súbor | Úloha |
|---|---|
| \`scanner_orb_v1.py\` | Samostatný simulátor, iba READ trhu |
| \`.github/workflows/orb_scanner.yml\` | Automatické skenovanie, prvý štart 09:20 NY, záložný 09:30 NY |
| \`orb_state_v1.json\` | Dnešný priebežný stav symbolov |
| \`orb_results_v1.csv\` | Unikátne uzavreté hypotetické obchody vrátane dôvodu výstupu |
| \`tests/test_orb_v1.py\` | Offline regresné testy stratégie |
| \`.github/workflows/orb_tests.yml\` | Automatický test pri zmenách |

GitHub Action kontroluje US kalendár vrátane skrátených dní.
Pri chybách API znova skúša ďalší cyklus. Ak GitHub nespustí žiaden beh,
nebudú nové záznamy; simulácia nevykonáva náhradné obchodné objednávky.

## Ako budeme ORB vyhodnocovať

Oddelene od FAST a BREAKOUT: počet virtuálnych obchodov, ziskovosť,
úspešnosť, priemerný obchod, počet nejednoznačných SL/TP sviečok,
maximálny drawdown a výsledky podľa času či symbolu.

**Pravidlá najprv nemeníme na základe 1–2 obchodov.**
Prvé orientačné vyhodnotenie až po väčšom počte obchodov (napr. 50–100)
a neskôr test na inom trhovom období.

## Obmedzenia dát

IEX nie je celý US trh. Pri riedkych IEX dátach môže chýbať niektorá
opening-range sviečka a ORB príslušnú akciu správne vynechá.
Virtuálne ceny *nie sú* skutočne dosiahnuteľné plnenia na burze.
Na živé obchodovanie by bolo nutné samostatne preveriť obchodovateľnosť
a shortovateľnosť titulov, spread, rýchlosť signálov aj stop objednávky.
