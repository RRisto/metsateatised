# Metsateatiste süsinikumõju MVP

Iseseisev Streamlit-rakendus. See **ei sõltu `et-landuse` repost**. Sealt on üle võetud ainult selle MVP jaoks vajalikud süsiniku koefitsiendid ja Metsaregistri detail-API kasutamise loogika.

Rakendus:

1. loeb Metsaregistri avalikust WFS-ist kehtivad ja arhiveeritud metsateatised;
2. filtreerib need valitud ajavahemikku;
3. leiab metsateatistega kattuvad metsaeraldised;
4. küsib Metsaregistri detail-API-st ainult nende eraldiste puuliigid, vanuse ja tagavara;
5. arvutab biomassi süsiniku **puuliikide kaupa**;
6. kuvab tulemused kaardil, KPI-dena, puuliikide koondina ja tabelis.

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

## Oluline piirang

`carbon_co2e_t` on hinnang **praegu eluspuude biomassis olevale CO₂e-le raiutaval alal**. See ei ole veel lageraie täielik netokliimamõju.

Järgmine mudeliversioon peaks võrdlema kahte trajektoori:
- ilma raieta;
- lageraie + metsauuendus;

ning nende vahet 10/30/50/100 aasta jooksul, lisades vähemalt raiutud puittooted, surnud orgaanilise aine ja mullasüsiniku.
