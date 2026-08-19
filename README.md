# Metsateatiste biomassi süsiniku MVP

Iseseisev Streamlit-rakendus. See **ei sõltu `et-landuse` repost**. Sealt on üle võetud ainult selle MVP jaoks vajalikud süsiniku koefitsiendid ja Metsaregistri detail-API kasutamise loogika.

Rakendus:

1. loeb Metsaregistri avalikust WFS-ist kehtivad ja arhiveeritud metsateatised;
2. filtreerib need valitud ajavahemikku;
3. leiab metsateatistega kattuvad metsaeraldised;
4. küsib Metsaregistri detail-API-st ainult nende eraldiste puuliigid, vanuse ja tagavara;
5. arvutab **elusbiomassi süsinikuvaru** ja **kavandatava raiemahu biomassi**
   puuliikide kaupa ning hoiab need eraldi;
6. kuvab tulemused koos mahuallika, inventuuri värskuse, ruumilise andmekatte ja
   jooksva juurdekasvu teabega kaardil, KPI-dena, koondina ja tabelis.

## Käivitamine

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
streamlit run app.py
```

Alternatiivina saab sõltuvusi ja rakendust hallata `uv`-ga:

```bash
uv sync
uv run streamlit run app.py
```

## Arenduskontrollid

```bash
uv run ruff check .
uv run pytest
```

## Kohalik andmevahemälu

Rakendus salvestab Metsaregistri WFS-vastused kausta `data/cache/wfs` ja eraldiste
detailvastused kausta `data/cache/details`. WFS-andmeid kasutatakse kuni 24 tundi
ja detailandmeid kuni 30 päeva. Külgriba nupp **Värskenda lähteandmeid** kustutab
nii ketta- kui ka Streamliti vahemälu; järgmine **Laadi ja arvuta** päring laadib
andmed uuesti.

Eraldised leitakse esmalt teatise `katastri_nr` ja `eraldise_nr` järgi. Kui täpset
vastet ei leita, kasutatakse ainult selle teatise väikest ruumilist bbox-päringut.
Unikaalsed eraldiseviited saadetakse WFS-i kuni 50 viite kaupa, mitte ühe päringuna
iga teatise kohta. Rakendus näitab eraldisepakkide ja detailvastuste edenemist.

## Metsateatiste ajalooline sünkroonimine

Külgriba jaotises **Metsateatiste andmete sünkroonimine** käivita töövoog nupuga
**Laadi/uuenda metsateatised**. See laadib Metsaregistri WFS-ist toored kehtivad
ja arhiveeritud metsateatised eraldi püsivasse andmehoidlasse.
Esimesel allalaadimisel vali võimalikult pikk vajalik ajalooline kuupäevavahemik.
Järgnevatel kordadel vali ainult juba salvestatud katvusele järgnev ajavahemik;
valmis kuude partitsioonid jäetakse siis vahele.

Juba alla laaditud kuude tahtlikuks uuendamiseks vali ülekattega ajavahemik **ja**
lülita sisse märkeruut **Uuenda ka juba laaditud kattuvaid kuid**. Ilma selle
märkeruuduta jäetakse valmis partitsioonid ka ülekattega kuude korral vahele;
tavalisel inkrementaalsel sünkroonimisel pole seda vaja sisse lülitada.
Toorandmed paiknevad kihiti teedel
`data/notices/<layer>/year=YYYY/month=MM/notices.parquet`.

Sünkroonimine laadib ainult toormetsateatised. Puistu eraldiste sidumine,
biomassi arvutamine ning ühine suuremahuline visualiseerimine on eraldi töö ja
ei käivitu selle toiminguga. Lühiajalise `data/cache` kustutamine ei kustuta
püsivat metsateatiste andmehoidlat `data/notices`.

## Kümne aasta andmete eeltöötlus

Olemasolevate toorpartitsioonide eraldistega sidumiseks ja biomassi arvutamiseks käivita:

```powershell
.venv\Scripts\python.exe preprocess_notices.py
```

Käsk ei laadi metsateatisi uuesti ega muuda `data/notices` sisu. See küsib võrgust ainult
eraldised ja nende detailid ning avaldab iga valmis kalendrikuu eraldi teedele
`data/processed/notices/year=YYYY/month=MM/`. Töö võib katkestada ja sama käsuga jätkata;
valmis kuud jäetakse vahele.

Ühe kuu proovikäivitus:

```powershell
.venv\Scripts\python.exe preprocess_notices.py --start 2016-08 --end 2016-08
```

Uuesti arvutamiseks kasuta `--force`. Detailpäringute vaikimisi paralleelsus on 12 ja seda saab
muuta valikuga `--detail-workers`. Võrguveaga kuu raporteeritakse, järgmiste kuude töötlemine
jätkub ning käsu väljumiskood on vea korral 1.

Valmis andmeid saab uurida märkmikus `notebooks/notice_exploration.ipynb`. Laadurid
`load_processed_notices` ja `load_processed_species` loevad ainult manifestis valminuks märgitud
kuud ning toetavad kuuvahemikku ja veergude projektsiooni.

## Andmeallikad

Metsaregistri GeoServer WFS:
`https://gsavalik.envir.ee/geoserver/metsaregister/ows`

Kihid:
- `metsaregister:teatis`
- `metsaregister:teatis_arhiiv`
- `metsaregister:eraldis`

Eraldise detailid:
`https://register.metsad.ee/portaal/api/rest/eraldis/detail/{eraldis_id}`

## Süsinikuarvutus

Puuliigiti:

`tüvemaht × puidutihedus × BEF (1.30) × süsinikufraktsioon (0.50) × 44/12`

Puidutihedused, mida MVP kasutab:

- mänd 0.42
- kuusk 0.40
- kask 0.51
- haab 0.35
- sanglepp 0.45
- hall lepp 0.45
- saar 0.57
- tamm 0.58

Kõik ühikud on kooskõlas arvutusega, kus tüvemaht on m³ ja tulemus tonnides CO₂e.

## Tulemuste väljad

`standing_live_biomass_tco2` kirjeldab inventuuri tagavarast hinnatud praegust
eluspuude biomassi raieteatise ja eraldiste kattel. Selle pindalapõhine paariline on
`standing_live_biomass_tco2_ha`. `planned_harvest_biomass_tco2` kirjeldab eraldi
teatises esitatud kavandatava raiemahu biomassi. Kavandatava raiemahu biomass ei
ole heite ega kliimamõju hinnang ning seda ei asendata puistu praeguse tagavaraga.

Mahu päritolu on alati üks neljast sildist:

- `detail-liigiline tagavara` — elusbiomassi maht pärineb eraldise detailandmete
  puuliigipõhisest tagavarast;
- `eraldise tagavara + liigiosakaal` — elusbiomassi maht pärineb eraldise
  kogutagavarast ja on jaotatud puuliikide osakaalude järgi;
- `raiemahu põhine hinnang` — kavandatava raiemahu biomass põhineb teatises
  esitatud raiemahul ja puuliikide osakaaludel;
- `andmed puuduvad` — vastava mahu arvutamiseks ei olnud piisavalt andmeid.

`standing_volume_basis` ja `planned_harvest_volume_basis` hoiavad kahe hinnangu
päritolu eraldi. `volume_source_quality` kirjeldab elusbiomassi mahuallikat, mitte
koondhinnet.

Andmekvaliteet esitatakse sõltumatute mõõtmetena:

- `inventory_date`, `inventory_age_years` ja `inventory_recency` kirjeldavad
  inventuuri aega. Värskuse klassid on 0–2 aastat `väga hea`, 3–5 `hea`, 6–8
  `vananev`, 9+ `nõrk` ning puuduva kuupäeva korral `teadmata`;
- `spatial_coverage_pct` ja `spatial_coverage_quality` kirjeldavad, kui suur osa
  teatise pindalast on eraldiste inventuuriandmetega kaetud. Piirid on vähemalt
  90% `hea`, vähemalt 50% `osaline` ja alla 50% `nõrk`;
- `mean_inventory_age_years` on puuliikide mahu järgi kaalutud keskmine vanus
  inventuuri hetkel. `mean_current_age_years` on eraldi puuliikide mahu järgi
  kaalutud keskmine jooksev vanus. Inventuurivanust ja jooksvat vanust ei segata;
- `current_increment_m3_ha_y` on inventuuri hetkeseisu pindalapõhine jooksev
  juurdekasv ja `current_increment_on_overlap_m3_y` sama näitaja kattuva ala
  kohta. `current_increment_coverage_pct` ja `current_increment_is_complete`
  näitavad, kas juurdekasvu koond on täielik. Puuduvat juurdekasvu ei tõlgendata
  nullina.

Rakenduse tabel ja CSV kasutavad neid selgeid väljanimesid ega avalda varasemaid
mitmetähenduslikke koondvälju.

## Oluline piirang

Rakendus hindab biomassi hetkeseisu ja kavandatud raiemahus sisalduvat biomassi,
mitte heidet ega kliimamõju. Jooksvat juurdekasvu ei kasutata tulevase tagavara
ekstrapoleerimiseks, sest üks aastane näitaja ei kirjelda usaldusväärselt kasvu,
suremust, raiet ega uuenemist ajas.

Trajektooride simulatsioon on seetõttu edasi lükatud eraldi mudelisse. See peab
võrdlema vähemalt raie puudumist ning raiet koos metsauuendusega 10/30/50/100
aasta jooksul ja lisama raiutud puittooted, surnud orgaanilise aine ning
mullasüsiniku.
