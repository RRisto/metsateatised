# Kiire ajaloolise metsateatiste töölaua disain

## Eesmärk

Rakendus peab võimaldama kord juba laaditud ja arvutatud kuni kümne aasta
metsateatiste andmeid kiiresti uuesti avada, filtreerida, graafikutel kuvada ja
kaardil uurida. Metsaregistri päringuid, ruumilisi lõikeid ja süsinikuarvutust ei
tohi korrata, kui sama lähteandmestik ja sama arvutusmudeli versioon on juba
töödeldud.

Siht on säilitada olemasolev Streamliti kasutajaliides ja arvutussemantika, kuid
eraldada aeglane andmetöötlus kiirest interaktiivsest vaatest.

## Jõudluse vastuvõtukriteeriumid

- Salvestatud ajaloolise andmestikuga töölaud avaneb tavalisel arendusmasinal
  kuni kolme sekundiga.
- Aasta, raieliigi, puuliigi või piirkonna filtri muutuse järel uuenevad
  koondgraafikud kuni ühe sekundiga.
- Kaardi liigutamise või suumimise järel ilmub sobiva detailsusega kiht kuni kahe
  sekundiga.
- Brauserisse ei saadeta ühe kaardivastusega üle 5 000 detailse polügooni.
- Eesti üldvaade ei sõltu metsateatiste koguarvust lineaarselt: üldvaade kuvab
  fikseeritud 5 × 5 km koondruudustikku.
- Korduv avamine ei tee Metsaregistri võrgupäringuid ega süsinikuarvutust, kui
  kasutaja pole valinud andmete värskendamist või arvutusmudeli versioon pole
  muutunud.

## Põhimõtted ja piirid

- Süsinikuarvutuse valemid ja tähendus ei muutu selle töö raames.
- Elusbiomassi süsinikuvaru ja kavandatava raiemahu biomass jäävad eraldi
  näitajateks ega muutu heite või pikaajalise kliimamõju hinnanguks.
- Algne täisgeomeetria säilib kettal. Lihtsustatud geomeetriat kasutatakse ainult
  visualiseerimiseks.
- GeoParquet on püsiv failivorming. DuckDB on lokaalne päringu- ja
  koonduskiht; eraldi andmebaasiserverit ei lisata.
- Streamlit jääb kasutajaliidese raamistikuks.
- Vektorpaanide serverit ega pilveandmebaasi esimeses teostuses ei lisata.

## Arhitektuur

Süsteem jaguneb neljaks vastutusalaks.

1. **Import ja inkrementaalne värskendus** laadib Metsaregistrist ainult puuduvad
   või kasutaja valitud ajavahemikud, normaliseerib väljad ning kirjutab
   partitsioneeritud GeoParquet-andmestiku.
2. **Arvutustulemuste hoidla** seob iga töödeldud rea lähteandmete ja
   arvutusmudeli versiooniga. Mudeliversiooni muutus tühistab ainult arvutatud
   veerud ja neist sõltuvad koondid, mitte algandmete allalaadimise.
3. **Koondpäringud** kasutavad DuckDB-d, et lugeda Parquet-faile otse kettalt ja
   tagastada graafikutele väikesed tabelid.
4. **Mitme detailsusega kaart** valib suumitaseme ja nähtava kaardiakna põhjal
   koondruudud, lihtsustatud geomeetria või täpsed polügoonid.

Streamliti skript ei laadi käivitamisel kogu kümne aasta GeoDataFrame'i mällu.
See avab ühe taaskasutatava DuckDB ühenduse ning küsib iga komponendi jaoks ainult
vajalikud read ja veerud.

## Andmehoidla

### Failipaigutus

Andmed asuvad olemasoleva `data/cache` juure all:

```text
data/cache/historical/
  manifest.json
  notices/year=YYYY/month=MM/*.parquet
  calculations/model=<version>/year=YYYY/month=MM/*.parquet
  aggregates/model=<version>/annual.parquet
  aggregates/model=<version>/monthly.parquet
  aggregates/model=<version>/cutting_type.parquet
  aggregates/model=<version>/species.parquet
  map/model=<version>/grid_5km.parquet
  map/model=<version>/simplified/*.parquet
```

`manifest.json` kirjeldab kaetud ajavahemikke, viimast edukat värskendust,
lähtekihtide skeemi räsi, arvutusmudeli versiooni ja iga artefakti valmidust.
Manifest kirjutatakse viimasena atomaarse failivahetusega. Poolik import või
arvutus ei tohi muutuda kasutajale nähtavaks valmis andmestikuks.

### Partitsioneerimine

Metsateatised ja arvutustulemused partitsioneeritakse teatise kuupäeva aasta ja
kuu järgi. See võimaldab:

- laadida kümne aasta andmestikule ainult uue kuu;
- lugeda perioodifiltri korral ainult vajalikke faile;
- asendada ebaõnnestunud või värskendatud perioodi kogu hoidlat ümber kirjutamata.

Teatise stabiilne identifikaator on duplikaatide eemaldamise võti. Kui
lähtekihil identifikaator puudub, kasutatakse olemasoleva normaliseerimisega
kooskõlas geomeetria ja lähteatribuutide deterministlikku räsi.

## Inkrementaalne import ja arvutus

Kasutaja valib perioodi ja vajutab laadimisnuppu. Rakendus võrdleb perioodi
manifestiga.

- Täielikult kaetud periood: võrgupäringuid ei tehta.
- Osaliselt kaetud periood: laaditakse ainult puuduvad kuud.
- Sunnitud värskendus: valitud kuude lähtepartitsioonid laaditakse uuesti.
- Muutunud lähteandmete skeem: import peatub arusaadava veateatega; olemasolev
  viimane terviklik andmestik jääb kasutatavaks.

Pärast uute lähtepartitsioonide kirjutamist arvutatakse süsinikuveerud ainult
uutele või muutunud ridadele. Mõjutatud kuu-, aasta-, raieliigi-, puuliigi- ja
kaardikoondid ehitatakse uuesti. Teiste kuude artefakte ei muudeta.

Arvutusmudeli versioon on koodis eksplitsiitne konstant. Versiooni muutmisel
säilivad lähtepartitsioonid, kuid uue versiooni arvutused ja koondid ehitatakse
eraldi kataloogi. Eelmise versiooni faile võib hoida kuni uue versiooni eduka
valmimiseni.

## Graafikute päringukiht

Graafikud ei loe detailseid geomeetriaid. DuckDB päringud tagastavad ainult
vajalikud koondveerud ja rakendavad valitud perioodi, raieliigi, puuliigi ning
piirkonna filtrid.

Põhikoondid on:

- aasta ja kuu: teatiste arv, pindala, elusbiomassi süsinikuvaru ning
  kavandatava raiemahu biomass;
- raieliik: samad summad ja osakaalud;
- domineeriv puuliik: samad summad ja osakaalud;
- kvaliteedinäitajad: andmekatte, inventuuri värskuse ja mahuallika jaotused.

DuckDB ühendus luuakse `st.cache_resource` abil. Väikesed päringutulemused
vahemällu salvestatakse `st.cache_data` abil võtmega, mis sisaldab filtreid,
manifesti andmeversiooni ja arvutusmudeli versiooni. Andmete värskendus muudab
manifesti versiooni ning tühistab vanad päringutulemused.

## Mitme detailsusega kaart

### Üldvaade

Eesti üldvaates kuvatakse EPSG:3301 koordinaatsüsteemis 5 × 5 km ruudud. Iga
ruut sisaldab vähemalt:

- teatiste arvu;
- kattuva raieala kogupindala;
- elusbiomassi süsinikuvaru summat;
- kavandatava raiemahu biomassi summat;
- valdavat raieliiki ja selle osakaalu.

Ruudu värv sõltub kasutaja valitud režiimist. Süsinikurežiim kasutab koondväärtuse
intensiivsust; raieliigi režiim kasutab valdava raieliigi kategoorilist värvi.
Legend ütleb selgelt, et tegemist on koondruutudega.

### Piirkonnavaade

Keskmisel suumitasemel küsitakse ainult nähtava kaardiakna objekte ja kasutatakse
EPSG:3301 süsteemis topoloogiat säilitavalt lihtsustatud geomeetriat
10-meetrise tolerantsiga. Vastuvõtutest peab kinnitama, et pindalaarvutused
kasutavad endiselt täisgeomeetriat.

### Lähivaade

Lähivaates tagastatakse nähtava kaardiakna täpsed polügoonid koos olemasolevate
tööriistavihjete ja hüpikakendega. Kui nähtavas alas on üle 5 000 objekti, jääb
rakendus lihtsustatud kihile ja palub kasutajal lähemale suumida. Täisgeomeetria
ei lähe brauserisse enne, kui objektide piir on täidetud.

Kaardipäring saab sisendiks kaardiakna `west`, `south`, `east`, `north` piirid,
suumitaseme, värvirežiimi ja aktiivsed filtrid. Tagastus on üks GeoJSON
FeatureCollection, mitte üks Foliumi kiht objekti kohta.

## Kasutajaliides

Kasutajaliides eristab kolme olekut:

- **Salvestatud andmed** – tulemused loeti kohalikust hoidlast;
- **Uuendatakse** – alla laaditakse või arvutatakse ainult puuduv periood;
- **Koondvaade** – kaart kuvab ruute või lihtsustatud alasid, mitte
  täispolügoone.

Kasutaja näeb manifesti viimase värskenduse aega, kaetud perioodi ja aktiivset
arvutusmudeli versiooni. Olemasolev „Arvuta uuesti” valik jagatakse kaheks
selgeks toiminguks:

- „Värskenda valitud perioodi lähteandmed”;
- „Arvuta süsinikutulemused uuesti”.

Kaardi värvirežiimi valik „Süsinikuvaru” / „Raieliik” säilib kõigil
detailsustasemetel.

## Veakindlus

- Kõik uued partitsioonid ja koondid kirjutatakse ajutise nimega ning
  avaldatakse atomaarse failivahetusega.
- Ebaõnnestunud värskendus ei kustuta viimast terviklikku andmestikku.
- Vigane või puuduva artefaktiga manifest põhjustab mõjutatud artefakti
  taastamise, mitte vaikse tühja tulemuse.
- DuckDB päringuvea korral kuvatakse kasutajale päringu kontekst ja võimalus
  salvestatud andmestik uuesti avada.
- Üle 5 000 detailse objekti korral ei proovita brauserit üle koormata.
- Vahemälu puhastamine eristab tooreid lähteandmeid, arvutustulemusi,
  koondeid ja ajutisi Streamliti päringutulemusi.

## Testimisstrateegia

### Ühiktestid

- manifesti kaetud ja puuduva perioodi arvutus;
- partitsioonivõtmed ja duplikaatide eemaldamine;
- arvutusmudeli versiooni invalidatsioon;
- 5 × 5 km ruudustiku deterministlik määramine;
- kaardi detailsustaseme valik suumi ja objektide arvu järgi;
- filtri parameetrite muutumine päringuvahemälu võtmes.

### Integratsioonitestid

- esimene kümne aasta import kirjutab tervikliku hoidla;
- teine sama perioodi avamine ei kutsu võrku ega süsinikuarvutust;
- ühe uue kuu lisamine muudab ainult vastavaid partitsioone ja koondeid;
- mudeliversiooni muutus taaskasutab lähteandmeid, kuid mitte vana arvutust;
- graafikukoondid võrduvad detailandmetest käsitsi tuletatud kontrollsummadega;
- kaardi kõik kolm detailsusastet säilitavad värvirežiimi ja filtrid.

### Jõudlustestid

Kontrollandmestik sisaldab vähemalt 100 000 sünteetilist teatist ja realistliku
tippude arvuga polügoone. Test mõõdab eraldi DuckDB päringut, GeoJSON koostamist
ja HTML-i serialiseerimist. Vastuvõtupiirid on selle dokumendi jõudluse
vastuvõtukriteeriumid; test raporteerib ajad ja brauserisse saadetava payload'i
suuruse.

## Migratsioon

Olemasolev päringupõhine GeoParquet-tulemuste vahemälu jääb esimeses etapis
loetavaks. Kui sama perioodi fail on olemas, imporditakse see ajaloolisse
hoidlasse ilma Metsaregistri korduspäringuta. Import kontrollib kohustuslikke
veerusid, CRS-i ja arvutusmudeli versiooni. Sobimatu vana fail jäetakse alles,
kuid seda ei märgita manifestis valmis partitsiooniks.

Migratsioon ei kustuta olemasolevaid `data/cache` faile automaatselt. Kasutaja
võib need eemaldada alles pärast uue hoidla kontrollitud valmimist.

## Teostuse etapid

1. Manifest, partitsioneeritud hoidla ja olemasoleva vahemälu migratsioon.
2. Inkrementaalne import ning mudeliversiooniga arvutuste invalidatsioon.
3. DuckDB graafikukoondid ja Streamliti filtrid.
4. 5 × 5 km üldvaate koondkiht.
5. Nähtava ala lihtsustatud ja täpsed kaardikihid.
6. Jõudlusmõõtmised, veakindlus ja kasutajaliidese olekute viimistlus.
