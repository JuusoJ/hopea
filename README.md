# Hopea-signaali

Python-ohjelma, joka hakee hopeaan liittyvät markkinaindikaattorit ilmaisista
rajapinnoista, laskee niistä läpinäkyvän **nousu / neutraali / lasku** -jakauman
ja kirjoittaa tuloksen yhdeksi HTML-sivuksi kuvaajineen. Sivu toimii sellaisenaan
GitHub Pagesissa, ja GitHub Actions päivittää sen automaattisesti.

```
Hopea 47,00 $ (+1,42 %) | Päiväkauppa ▲82 % (P↑ 57 %) | Lyhyt ▲79 % (P↑ 59 %) | Pitkä ▲75 % (P↑ 53 %)
```

## Pika-aloitus omalla koneella

```bash
pip install -r requirements.txt
python hopea.py                # yksi ajo  -> site/index.html
python hopea.py --loop 60      # päivittää minuutin välein, sivu latautuu itse
python hopea.py --demo         # keksityllä datalla, ei verkkoa (ulkoasun testaus)
```

Avaa `site/index.html` selaimessa. Silmukka-ajossa sivu latautuu uudelleen samalla
välillä kuin ohjelma ajaa, ja konsoliin tulostuu yhden rivin yhteenveto joka kierroksella.
Hidas data (päiväkurssit, korko, COT) haetaan välimuistista harvemmin, joten minuutin
tahti ei kuormita rajapintoja turhaan.

Valitsimet: `--out` (HTML-tiedosto), `--json` (raakadata myös JSONina), `--refresh`
(selaimen uudelleenlatausväli sekunteina, 0 = pois), `-v` (lisää lokitusta).

## Julkaisu GitHub Pagesiin

1. Luo GitHubiin uusi repositorio (esim. `hopea-signaali`) ja vie sinne kaikki
   tämän kansion tiedostot, myös `.github/workflows/paivita.yml`.
2. Repositorion **Settings → Pages → Build and deployment → Source:** valitse
   **GitHub Actions**.
3. Mene **Actions**-välilehdelle, valitse *Päivitä hopea-signaali* ja paina
   **Run workflow**. Ensimmäinen ajo kestää noin minuutin.
4. Sivu löytyy osoitteesta `https://<käyttäjänimesi>.github.io/hopea-signaali/`.

Tämän jälkeen sivu päivittyy itsestään sunnuntaista perjantaihin. Varman 15 minuutin tahdin saat alla olevalla ulkoisella ajastuksella.

Jos *Tallenna signaaliloki* -vaihe epäonnistuu virheeseen 403, avaa
**Settings → Actions → General → Workflow permissions** ja valitse
**Read and write permissions**.

### Luotettava ajastus (suositus)

GitHubin oma ajastin ei ole luotettava: ruuhka-aikoina ajot viivästyvät tai jäävät
kokonaan väliin, eikä siitä tule ilmoitusta. Varmin ratkaisu on antaa ilmainen
ulkoinen palvelu (esim. [cron-job.org](https://cron-job.org)) käynnistää työnkulku
GitHubin rajapinnan kautta. GitHubin oma ajastin jää varajärjestelmäksi.

1. **Tee avain:** GitHub → profiilikuva → Settings → Developer settings →
   Personal access tokens → **Fine-grained tokens** → Generate new token.
   Repository access: **Only select repositories** → tämä repo.
   Permissions → Repository permissions → **Actions: Read and write**. Muita
   oikeuksia ei tarvita.
2. **Tee ajastus cron-job.orgissa:** Create cronjob
   - URL: `https://api.github.com/repos/<käyttäjä>/<repo>/actions/workflows/paivita.yml/dispatches`
   - Schedule: 15 minuutin välein
   - Advanced → Request method **POST**, body `{"ref":"main"}`
   - Headers: `Authorization: Bearer <avain>`, `Accept: application/vnd.github+json`,
     `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
3. **Testaa** (Test run): vastauksen pitää olla **204**, ja Actions-välilehdelle
   ilmestyy uusi ajo.

Sivun yläreunassa näkyy, kuinka vanhoja tiedot ovat. Jos tiedot ovat markkinan
ollessa auki yli 40 minuuttia vanhoja, sivulla näkyy varoitus.

**Hyvä tietää:**
- GitHubin ajastetut ajot ovat vähintään 5 minuutin välein ja voivat ruuhkassa
  viivästyä. Minuutin tarkkuuteen käytä paikallista `--loop 60` -ajoa.
- Jos haluat käyttää FREDin virallista rajapintaa, hanki ilmainen avain
  (fred.stlouisfed.org → My Account → API Keys) ja lisää se repoon salaisuudeksi
  nimellä `FRED_API_KEY` (Settings → Secrets and variables → Actions). Ilman avainta
  ohjelma käyttää FREDin avointa CSV-latausta.

## Kolme aikajännettä

Mittarit ennakoivat eri pituisia liikkeitä, joten ne lasketaan kolmena erillisenä lukemana:

| Aikajänne | Testataan | Mittarit ja painot |
|---|---|---|
| **Päiväkauppa** | hinta noin 4 h päästä (16 × 15 min) | päivänsisäinen vire 60 %, hopean trendi 25 %, RSI 15 % |
| **Lyhyt** (1–2 vk) | hinta 5 kaupankäyntipäivän päästä | dollari 20 %, reaalikorko 20 %, kupari 15 %, kulta 15 %, kulta–hopea-suhde 10 %, trendi 10 %, COT 5 %, RSI 5 % |
| **Pitkä** (1–3 kk) | hinta 40 kaupankäyntipäivän päästä | kulta–hopea-suhde 35 %, pitkä trendi 25 %, COT 20 %, reaalikorko 20 % |

Päiväkaupan trendi ja RSI otetaan edellisen päivän päätöskurssista, jotta historiatesti
ei käytä tulevaa tietoa.

## Indikaattorit

| # | Mittari | Lähde | Logiikka |
|---|---|---|---|
| 1 | Kulta–hopea-suhde | Yahoo (GC=F / SI=F) | Korkea ja laskeva suhde → nousua |
| 2 | Dollari-indeksi DXY | Yahoo (DX-Y.NYB) | Heikkenevä dollari → nousua |
| 3 | USA 10 v reaalikorko | FRED DFII10 | Laskeva reaalikorko → nousua |
| 4 | Kupari | Yahoo (HG=F) | Nouseva kupari (teollisuuskysyntä) → nousua |
| 5 | Kulta | Yahoo (GC=F) | Nouseva kulta → nousua |
| 6 | Rahastojen positiot | CFTC COT (Managed Money) | Vastavirtaan: ääri-ostopositio → laskua |
| 7 | Hopean trendi | Yahoo (SI=F) | 10 pv muutos ja etäisyys 20 pv keskiarvosta |
| 8 | Pitkä trendi | Yahoo (SI=F) | 60 pv muutos ja etäisyys 200 pv keskiarvosta |
| 9 | RSI 14 | Yahoo (SI=F) | Yli 70 → laskua, alle 30 → nousua |
| 10 | Päivänsisäinen vire | Yahoo 15 min, 60 pv | EMA9 vs. EMA21 ja hinta vs. VWAP |

Lisäksi sivu näyttää päiväkauppiaalle volatiliteetin (ATR 14 pv) eli tyypillisen
päiväliikkeen dollareina. Se ei vaikuta prosentteihin.

## Laskentakaava

Jokainen mittari antaa pistemäärän `s` väliltä −1…+1. Pistemäärät perustuvat
z-lukuihin (muutos jaettuna sen tavanomaisella vaihtelulla), jotka puristetaan
tanh-funktiolla välille −1…+1. Aikajänteen lukema:

```
Nousu     = Σ wᵢ · max(sᵢ, 0)
Lasku     = Σ wᵢ · max(−sᵢ, 0)
Neutraali = Σ wᵢ · (1 − |sᵢ|)
```

**Yksimielisyys ei ole todennäköisyys.** Mittarit mittaavat osin samaa asiaa, joten
niiden yksimielisyys liioittelee varmuutta. Siksi nettolukema `x = nousu − lasku`
muunnetaan historiallisesti toteutuneeksi todennäköisyydeksi:

```
P(hinta korkeammalla aikajänteen päästä) = 1 / (1 + e^−(a + b·x))
```

Luvut `a` ja `b` sovitetaan joka ajolla koko historiaan (logistinen regressio).
Sivu näyttää 90 %:n luottamusvälin ja kertoo, onko yhteys tilastollisesti
merkitsevä. Peräkkäiset havainnot menevät päällekkäin, joten epävarmuus on
kerrottu aikajänteen pituudella.

## Signaaliloki: mihin data tallentuu

Ohjelma lataa joka ajolla hintahistorian uudelleen, joten historiatestiä varten
mitään ei tarvitse tallentaa. **Signaaliloki** kirjaa sen sijaan jokaisen
etukäteen annetun ennusteen, jotta näet, miten malli on oikeasti toiminut:

| Tiedosto | Rivi | Sisältö |
|---|---|---|
| `signaaliloki_paiva.csv` | 1 / kaupankäyntipäivä, päätöskurssin jälkeen (klo 17 NY) | lyhyen ja pitkän aikajänteen lukemat ja todennäköisyydet, kaikkien mittarien pisteet, toteutunut muutos 5 ja 40 pv päästä |
| `signaaliloki_tunti.csv` | 1 / tunti markkinan ollessa auki | päiväkaupan lukema ja todennäköisyys, toteutunut muutos 4 h päästä |

- Tiedostot aukeavat suoraan Excelissä (erotin puolipiste, desimaalipilkku).
- Kirjattuja ennusteita ei muuteta. Vain toteutuneen muutoksen sarakkeet täytetään,
  kun aikajänne on kulunut.
- **Omalla koneella** lokit tallentuvat kansioon `loki/`. Toisen kansion voi valita
  valitsimella `--log-dir`, ja `--no-log` poistaa lokin käytöstä.
- **GitHubissa** loki tallentuu repon erilliseen `data`-haaraan, joka luodaan
  automaattisesti ensimmäisellä ajolla. Päähaaraan (`main`) ei tule bottitallennuksia,
  joten omat muutoksesi eivät törmää niihin. Tiedostot voi ladata myös sivun
  Seuranta-osiosta.
- Koska data-haaraan tulee tallennuksia, GitHub ei keskeytä ajastusta 60 päivän
  hiljaisuuden vuoksi.

## Säätäminen

Kaikki asetukset ovat `hopea.py`:n alussa:

- `HORIZONS` – aikajänteet, niiden mittarit, painot (`weights`, summa 1) ja testijänne (`forward`).
- `INDICATORS` – mittarien nimet ja selitystekstit.
- `SIGNAL_THRESHOLD` (0,85) ja `STRONG_THRESHOLD` (0,70) – signaalirajat.
- `CALL_THRESHOLD` (0,20) – seurannassa ennusteeksi lasketaan nettolukema vähintään ±20 %-yks.
- `INTRADAY_INTERVAL` – päivänsisäinen kynttiläväli (esim. `"5m"`, `"15m"`, `"1h"`).
- `TIMEZONE` – sivun kellonajat.

Sivun ulkoasu on tiedostossa `template.html` (HTML, CSS ja Plotly-kaaviot).

## Tärkeää tulkinnasta

- **Nousu/lasku-prosentit kuvaavat mittarien yksimielisyyttä, eivät takuuta.** 85 %:n
  nousulukema tarkoittaa, että lähes kaikki mittarit osoittavat ylös. Sen, mitä
  lukema on historiassa tarkoittanut, kertoo **historiallinen todennäköisyys**. Hopean
  lyhyen aikavälin suunnan ennustamisessa jo 55–60 % on hyvä tulos.
- Sivun **Toteutuiko?**-taulukko kertoo, kuinka usein hopea on historiassa todella
  liikkunut mallin osoittamaan suuntaan kullakin lukematasolla. Vertaa aina
  ”Kaikki päivät” -riviin: vasta sen selvästi ylittävä osuma kertoo, että malli tuo lisäarvoa.
- 85 %:n raja on tiukka. Jos taulukko näyttää, ettei rajaa ole juuri koskaan
  saavutettu, voit laskea `SIGNAL_THRESHOLD`-arvoa tai muuttaa painoja – ja tarkistaa
  muutoksen vaikutuksen samasta taulukosta.
- Korko- ja COT-tiedot on historiatestissä siirretty siihen päivään, jolloin ne oikeasti
  julkaistiin (ei kurkistusta tulevaan).
- Historiatesti on yleensä todellisuutta optimistisempi. Luotettavin mittari on
  **Seuranta**-osio, joka perustuu vain etukäteen kirjattuihin ennusteisiin.

## Rajoitukset

- Yahoo Finance on epävirallinen rajapinta: futuurihinnat voivat olla noin 10–15 min
  viiveellä, ja pilvipalveluista tehdyt haut joutuvat toisinaan rajoitusten kohteeksi.
  Ohjelma yrittää uudelleen, ja jos jokin lähde puuttuu, sen paino jaetaan muille ja
  sivulla näkyy ilmoitus. Jos hopean hintaa ei saada lainkaan, sivua ei päivitetä
  (edellinen versio jää näkyviin).
- Reaalikorko päivittyy kerran päivässä ja COT kerran viikossa (tiistain tilanne
  julkaistaan perjantaina).
- Reaaliaikaiseen dataan tarvitaan maksullinen lähde (esim. välittäjän API); vaihda
  silloin `_yahoo`-funktion sisältö.

## Vastuuvapaus

Laskennallinen apuväline, ei sijoitusneuvontaa. Indikaattorit kuvaavat menneitä ja
nykyisiä markkinaoloja; ne eivät takaa tulevaa hintakehitystä.
