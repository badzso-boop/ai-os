# 21. Static Subdomain Deploy Pipeline — `deploy_static.sh`

> **Státusz: tervdokumentum (design doc).** MÉG NINCS implementálva. Ez a
> dokumentum egy **determinisztikus deploy-scriptet** tervez, ami egy statikus
> bundle-t (jellemzően az `ai-os startup` kimenetét, 20. doc) egy izolált Docker
> nginx-konténerbe rakva élővé tesz egy subdomainen, a MEGLÉVŐ Cloudflare Tunnel
> + host nginx infrastruktúra mögött.
>
> ⚠️ **Ez a modul közvetlenül érinti Norbert ÉLŐ, MEGOSZTOTT otthoni szerverét**,
> ami `ujjweb.hu`-t és sok más valós projektet/ügyfél-oldalt szolgál ki EGYETLEN
> Cloudflare Tunnel + host nginx mögött. A dokumentum központi tervezési elve
> ezért: **additív-only, izolált, idempotens, visszavonható, és soha nem nyúl
> megosztott konfig-blokkokhoz.** Lásd a globális `~/.claude/CLAUDE.md` szabályait
> és a `homelab-shared-server-context` / `feedback-cautious-shared-infra` /
> `server-freeze-investigation` memóriákat.

---

## 0. ⚠️ A megosztott szerver — a nem-alkudható szabályok

Mielőtt bármi másról szó esne, ezek a kőbe vésett korlátok. A script tervének
minden döntése ezekből következik:

1. **SOHA ne szerkessz meglévő, megosztott konfigot.** Se meglévő nginx
   `server {}` blokkot, se a `cloudflared` ingress-listát, se a crontabot, se a
   hálózati configot. Kizárólag **új, egyedi nevű fájlokat adunk hozzá**
   (`/etc/nginx/conf.d/ai-os-startup-<sub>.conf`), amiket később nyom nélkül el
   lehet távolítani.
2. **Minden deploy izolált.** Saját Docker-konténer, saját port, saját nginx
   site-fájl, saját registry-bejegyzés. Egy deploy bukása/törlése egy másikat
   soha nem érinthet.
3. **Namespacing.** Minden AI-OS által létrehozott objektum
   `ai-os-startup-<subdomain>` névmintát visel (konténer, site-fájl, hálózat),
   pontosan mint a `container_runner`/`cleaner` saját névterei — így a
   `ai-os clean`-hez hasonló, biztonságos szelektív takarítás lehetséges.
4. **A konténer NEM publikus közvetlenül.** A port csak `127.0.0.1`-re publikált
   (`-p 127.0.0.1:<port>:80`) — kizárólag a host reverse proxyn át érhető el,
   nem a nyílt internetről, és nem a LAN felől.
5. **Validálj, aztán reload — SOHA restart.** Az nginx-újratöltés előtt kötelező
   `nginx -t` (config-teszt); ha az hibázik, a script **visszavonja** az új
   site-fájlt és nem tölt újra. Reload (`nginx -s reload` / `systemctl reload`),
   nem restart — a restart az egész hostot (minden más projektet) leszakítaná.
   Ugyanez a `cloudflared`-re, ha egyáltalán hozzá kell nyúlni (lásd wildcard).
6. **Root nélkül a konténerben, read-only mount, erőforrás-limit.** A statikus
   fájlok `:ro` mountolva; a konténer nem-root; `--memory`/`--cpus` korlát; nincs
   hozzáférése a host belső szolgáltatásaihoz.
7. **Dry-run alapból az első futásra + megerősítés.** A script `--dry-run`-nal
   megmutatja pontosan MIT csinálna (milyen fájlt ír, milyen konténert indít,
   reload-ol-e), mielőtt bármit tenne. Éles művelet emberi megerősítéshez kötött.
8. **Az AI soha nem futtatja ezt közvetlenül.** Az `ai-os startup` (vagy a
   felhasználó) hívja meg a scriptet paraméterrel; az LLM nem ad ki docker/nginx/
   cloudflared parancsot (kontextus-költség ÉS biztonság — egy determinisztikus,
   auditálható script sokkal védhetőbb egy live szerveren, mint egy AI ad-hoc
   parancsai).

---

## 1. Miért script, és nem AI-tool?

- **Kontextus-költség.** A deploy sok apró, olvasás-nehéz lépés (port keresés,
  config-írás, health-check, registry). Ha ezt az AI tool-hívásokkal csinálná,
  rengeteg tokent és tool-round-tripet enne — feleslegesen, hisz a lépések
  **determinisztikusak**.
- **Determinizmus & auditálhatóság.** Egy verziózott shell-script pontosan
  ugyanazt csinálja minden futáskor; git-diffelhető, reviewelhető, tesztelhető.
  Egy AI improvizált parancs-sorozata nem.
- **Biztonság a live hoston.** A legfontosabb ok. A megosztott szerveren egy
  rossz `nginx` vagy `docker` parancs sok valós oldalt vihet le. A script
  beépített védőkorlátai (validálás, additív-only, dry-run, rollback) garantálják,
  hogy ez ne történhessen meg — egy szabad AI-parancs nem.

---

## 2. A kérés útja (architektúra)

```
[Böngésző: https://freshbox.apps.ujjweb.hu]
        │  (TLS az edge-en)
        ▼
[Cloudflare edge]  ── proxied DNS (wildcard *.apps.ujjweb.hu → a tunnel) ──►
        │
        ▼
[cloudflared  (a MEGLÉVŐ tunnel, változatlanul)]
        │  ingress: hostname *.apps.ujjweb.hu → http://localhost:80 (host nginx)
        ▼
[HOST nginx  (:80/:443, a MEGLÉVŐ reverse proxy)]
        │  ÚJ additív site-fájl: server_name freshbox.apps.ujjweb.hu;
        │  location / { proxy_pass http://127.0.0.1:39001; }
        ▼
[Docker nginx konténer  ai-os-startup-freshbox  → 127.0.0.1:39001]
        │  a statikus bundle :ro mountolva /usr/share/nginx/html-be
        ▼
[a generált statikus site]
```

A kulcs a **wildcard** (2.1): egy egyszeri beállítás után **minden új deploy
tisztán additív és csak a host nginx conf.d-jét érinti** — se DNS-, se
cloudflared-, se megosztott-blokk-változás deploykor.

### 2.1. Egyszeri beállítás (one-time, emberi review-val)

Ezt **egyszer** kell megcsinálni, tudatosan, a megosztott infra ismeretében
(nem a script automatikusan, mert megosztott config):

1. **Dedikált wildcard zóna.** Válassz egy AI-OS-nek fenntartott aldomaint, pl.
   `*.apps.ujjweb.hu` (vagy `*.startups.ujjweb.hu`). Így az AI-OS deployok SOHA
   nem ütköznek a meglévő, kézzel kezelt subdomainekkel (`chatchat`, `wrenchly`
   stb.) — külön névtérben élnek.
2. **Cloudflare DNS.** Egy proxied wildcard `CNAME *.apps → <tunnel-uuid>.
   cfargotunnel.com` (vagy a tunnel route-ja). Egyszeri.
3. **cloudflared ingress.** EGY additív ingress-szabály a meglévő tunnel
   configban: `hostname: "*.apps.ujjweb.hu" → service: http://localhost:80` (a
   host nginx). Ez az EGYETLEN cloudflared-érintés, és egyszeri — utána minden
   subdomain a host nginxen dől el a `Host` fejléc alapján. (A meglévő ingress-
   szabályokat nem bántjuk; a catch-all `404`/`http_status` szabály elé, additív
   módon szúrjuk be.)
4. **Fenntartott nginx include-könyvtár + port-tartomány.** A
   `/etc/nginx/conf.d/ai-os-startup-*.conf` mintát az AI-OS deployok kizárólagos
   használatára tartjuk fenn, és egy **port-tartományt** (pl. `39000–39999`) is,
   amit más projekt nem használ.

Ezt a lépést a script **nem** végzi el magától — legfeljebb egy külön `deploy_
setup_check.sh` ellenőrzi, hogy a wildcard + tartomány rendben van-e, és
figyelmeztet, ha nincs. Az élő szerveren a megosztott config első beállítása
mindig tudatos, reviewelt emberi művelet (a globális CLAUDE.md szerint:
„investigate what else depends on it first and prefer additive changes").

### 2.2. Alternatíva (NEM ajánlott alapból): per-subdomain cloudflared ingress

Wildcard nélkül minden deploy egy-egy új `cloudflared` ingress-szabályt igényelne
a megosztott tunnel-configban — ez **minden deploykor a megosztott configot
szerkesztené**, ami sérti a 0. szabályokat. Ezért az **A) wildcard út az ajánlott**;
a per-subdomain ingress csak akkor jön szóba, ha wildcard valamiért nem opció, és
akkor is szigorúan additív, validált (`cloudflared ... validate`), reload-olt (nem
restart) módon, dokumentált rollbackkal.

---

## 3. A deploy script — `scripts/deploy_static.sh`

### 3.1. Paraméterek

```
deploy_static.sh --subdomain <név> --dir <statikus-bundle-útvonal>
                 [--zone apps.ujjweb.hu]     # a wildcard zóna (default konfigból)
                 [--port <n>]                # kézi port; alapból auto a tartományból
                 [--dry-run]                 # csak mutatja, mit tenne — semmit nem hajt végre
                 [--teardown]                # a subdomain deploy TELJES, additív-only lebontása
                 [--yes]                     # a megerősítő kérdés átugrása (CI-hez)
deploy_static.sh --list                      # az AI-OS deployok listája (registry)
```

### 3.2. Lépések (éles futás)

1. **Bemenet-validálás.**
   - `subdomain`: szigorú regex (`^[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?$`), kisbetű,
     nincs pont (egy szintű a zónán belül).
   - **Foglalt-név blocklist:** meglévő, kézzel kezelt hostok/aldomainek nevei
     (pl. `www`, `chatchat`, `wrenchly`, `portainer`, `jellyfin`, …) tiltottak,
     hogy véletlenül se lehessen egy éles szolgáltatás nevét „elfoglalni".
   - **Ütközés-ellenőrzés:** ha már van `ai-os-startup-<sub>` konténer/site, az
     **update** (idempotens újradeploy), nem hiba.
   - `dir`: létezik, tartalmaz `index.html`-t, csak statikus fájlok (nincs
     szerveroldali kód, ami félrevezetne — statikus serve).
2. **Port-allokáció (determinisztikus).** A `39000–39999` tartományból: ha a
   subdomain már a registryben van, az ő portját használja (idempotencia);
   különben a legkisebb szabad portot foglalja (a registry + `ss -ltn` alapján).
   Atomikus registry-írás (tempfile + `os.replace`, mint a `registry.py`).
3. **Konténer (izolált, hardened).**
   ```
   docker run -d --name ai-os-startup-<sub> \
     -v <dir-abszolút>:/usr/share/nginx/html:ro \
     -p 127.0.0.1:<port>:80 \
     --restart unless-stopped \
     --memory=128m --cpus=0.5 \
     --cap-drop=ALL --security-opt no-new-privileges \
     --read-only --tmpfs /var/cache/nginx --tmpfs /var/run \
     -v <security-headers.conf>:/etc/nginx/conf.d/default.conf:ro \
     nginx:alpine
   ```
   - Csak `127.0.0.1`-re publikált port → nem érhető el a proxyt kikerülve.
   - `:ro` mount, `--read-only` gyökér, `--cap-drop=ALL`, non-root — a statikus
     kiszolgáláshoz semmi több nem kell.
   - **A konténer nginx configja biztonsági fejléceket ad** (a site publikus a
     tunnelon át): `Content-Security-Policy` (a self-contained bundle-höz szabva),
     `X-Frame-Options: DENY` (vagy SAMEORIGIN), `X-Content-Type-Options: nosniff`,
     `Referrer-Policy`. A gzip/cache-header a statikus assetekre.
   - Ez egy **perzisztens serve-konténer** (`--restart unless-stopped`), NEM az
     ephemeral validációs sandbox — más életciklus, más cél. (A validáció a 20.
     doc szerint már lefutott a bundle-ön, mielőtt ideér.)
4. **Additív host-nginx site.** Új fájl:
   `/etc/nginx/conf.d/ai-os-startup-<sub>.conf`
   ```
   server {
     listen 80;
     server_name <sub>.apps.ujjweb.hu;
     location / {
       proxy_pass http://127.0.0.1:<port>;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
     }
   }
   ```
   (TLS-t az edge intézi Cloudflare-nél; a host↔konténer origin-forgalom a tunnelon
   belül http.)
5. **Validál, aztán reload (SOHA restart).**
   - `nginx -t` → ha **hibázik**, a script **törli az imént írt site-fájlt**,
     leállítja az új konténert, és hibával kilép — a host változatlan marad.
   - ha rendben: `nginx -s reload` (a futó workerek grace-full újratöltése — a
     többi oldal forgalma nem szakad meg).
6. **Health-check.** `curl -sf -H "Host: <sub>.apps.ujjweb.hu" http://127.0.0.1:80/`
   → 200-at kell adnia. Sikertelenség esetén rollback (4–5. visszavonása).
7. **Registry-bejegyzés.** `~/.ai-os/deploys.json` (az `AI_OS_HOME` alá, mint a
   `projects.json`): `{subdomain, port, container, site_file, dir, url, created_at}`
   — a `--list` és a `--teardown` innen dolgozik.
8. **Kiírja az élő URL-t:** `https://<sub>.apps.ujjweb.hu`.

### 3.3. Idempotencia és rollback

- **Újradeploy** ugyanarra a subdomainre: a portot újrahasználja, a bundle-t
  atomikusan cseréli (a mount ugyanaz a könyvtár; új tartalom = a fájlok cseréje,
  a konténer újraindítása nem is kell, mert a mount élő — vagy egy gyors
  `docker restart` a namespaced konténerre). A site-fájl változatlan.
- **Rollback minden lépésre.** Ha bármelyik lépés bukik, a script visszacsinálja
  az addigiakat (site-fájl törlés, konténer stop/rm, registry-visszaállítás) —
  úgy, hogy a **host mindig konzisztens** maradjon. A kritikus invariáns: **rossz
  nginx-config sosem kerül reload-ra** (a `nginx -t` kapu miatt), tehát egy hibás
  deploy nem viheti le a többi oldalt.

### 3.4. Teardown / list

- **`--teardown --subdomain <sub>`**: kizárólag a namespaced objektumokat bontja
  le — `docker rm -f ai-os-startup-<sub>`, az `ai-os-startup-<sub>.conf` törlése,
  `nginx -t` + reload, registry-bejegyzés törlése. Megosztott confighoz **nem
  nyúl**. (A wildcard DNS/cloudflared egyszeri beállítás megmarad — az közös
  infra, nem per-deploy.)
- **`--list`**: a registryből kiírja az élő AI-OS deployokat (subdomain, url,
  port, konténer, kor).
- **`ai-os clean` integráció:** a `cleaner.py` bővíthető úgy, hogy `--deploys`
  kapcsolóval a registry alapján listázza/takarítsa az elárvult
  `ai-os-startup-*` konténereket + site-fájlokat (ugyanaz az „csak a saját
  névterünk" elv, mint a sandbox-artefaktoknál).

---

## 4. Port-allokáció és registry

- **Fenntartott tartomány:** `39000–39999` (konfigurálható), amit más projekt nem
  használ a hoston. A tartomány fixálása egyszeri, dokumentált döntés.
- **Registry:** `~/.ai-os/deploys.json`, atomikus írás (tempfile + `os.replace`),
  a `registry.py` mintájára. Ez a single-source-of-truth a port↔subdomain
  leképezésről, a teardownról és a listázásról.
- **Ütközés-védelem:** foglaláskor a script a registry ÉS az élő portok
  (`ss -ltn`) unióját nézi, hogy soha ne foglaljon élő portot.

---

## 5. Biztonsági checklista (a script minden éles futására)

A script indításkor ellenőrzi és a `--dry-run` kiírja:

- [ ] a subdomain a fenntartott **wildcard zónában** van (nem a gyökér-domainen,
      nem egy kézzel kezelt subdomainen)
- [ ] a subdomain **nincs a foglalt-név blocklistán**
- [ ] a port a **fenntartott tartományban** van és szabad
- [ ] a konténer portja **`127.0.0.1`-re** publikált (nem `0.0.0.0`)
- [ ] a mount **`:ro`**, a konténer **non-root**, van erőforrás-limit
- [ ] **csak új, namespaced** nginx site-fájl készül; meglévő blokk nem módosul
- [ ] **`nginx -t` sikeres** a reload ELŐTT; hiba → rollback, nincs reload
- [ ] **reload, nem restart**
- [ ] a művelet a registrybe kerül (visszavonhatóság)

Amit a script **SOHA nem tehet**: meglévő nginx/cloudflared blokk szerkesztése,
nginx/cloudflared **restart**, `0.0.0.0`-ra publikálás, root konténer, a
fenntartott zónán/tartományon kívüli objektum létrehozása, a megosztott tunnel-
config per-deploy módosítása (wildcard mellett nincs is rá szükség).

---

## 6. Integráció az `ai-os startup`-pal

Az `ai-os startup --subdomain <név>` a sikeres validáció után **egyetlen script-
hívást** tesz: `deploy_static.sh --subdomain <név> --dir <bundle>`. Az AI ezen a
ponton kilép a képből — nem lát, nem futtat infra-parancsot. A script kimenete
(az élő URL vagy a hiba) visszakerül a CLI-be. Ez a felelősség-elválasztás a
lényeg: **az AI statikus fájlokat gyárt; a determinisztikus, hardened script
deployol; az ember (HITL preview) engedélyez.**

---

## 7. Miért biztonságos ez a live megosztott szerveren — összefoglalva

| Kockázat | Hogyan zárjuk ki |
| -------- | ---------------- |
| Egy deploy leviszi a többi oldalt | `nginx -t` a reload előtt + reload (nem restart) + rollback |
| Megosztott config elrontása | additív-only, csak új namespaced fájlok, wildcard = 0 per-deploy közös-config-érintés |
| Port/subdomain ütközés élő szolgáltatással | fenntartott zóna + tartomány + blocklist + registry |
| Konténer-kitörés / host-hozzáférés | non-root, `--cap-drop=ALL`, read-only, `:ro` mount, 127.0.0.1-port |
| Nyilvános közvetlen elérés a proxyt kikerülve | a port csak `127.0.0.1`-en publikált |
| Elárvult erőforrások crash után | registry + `--teardown` + `ai-os clean --deploys` (namespaced) |
| AI ad-hoc infra-parancsai | az AI nem futtat infra-parancsot; csak a script teszi |

---

## 8. Jövőbeli kiterjesztések

- **TTL / auto-lejárat.** A demó-subdomainek automatikus lebontása N nap után a
  registry `created_at`-ja alapján (egy `deploy_static.sh --gc` a crontabban —
  additív, namespaced).
- **Kvóta.** Max. párhuzamos AI-OS deploy szám (a fenntartott tartomány mérete
  amúgy is természetes felső korlát).
- **Alap-auth / „nem indexelendő".** Egy egyszerű Cloudflare Access szabály vagy
  `X-Robots-Tag: noindex` a demókra, hogy ne szivárogjanak ki idő előtt.
- **Statikus TLS-origin.** Ha valaha közvetlen (tunnel nélküli) elérés kellene,
  Cloudflare Origin Cert — de a jelenlegi tunnel-modell ezt feleslegessé teszi.
- **Blue/green demó-frissítés.** Új bundle egy második namespaced konténerbe,
  atomikus site-fájl-átkapcsolás, majd a régi lebontása — nulla-állásidős
  frissítés (opcionális, a demókhoz általában felesleges).

---

## Kapcsolódó dokumentumok

- `20_STARTUP_GENERATOR.md` — az `ai-os startup`, ami ezt a scriptet meghívja.
- `11_ORCHESTRATOR_TECH_STACK_AND_DEPL.md` — az AI-OS saját deploy/tech-stack
  megfontolásai.
- Globális `~/.claude/CLAUDE.md` + a `homelab-shared-server-context` /
  `feedback-cautious-shared-infra` / `server-freeze-investigation` memóriák — a
  megosztott szerver konkrét incidensei és megerősített mintái, amikből a 0.
  szabályok származnak.
```
