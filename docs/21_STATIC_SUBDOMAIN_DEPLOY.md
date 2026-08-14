# 21. Static Subdomain Deploy Pipeline — `deploy_static.sh`

> **Status: Design Document.** NOT YET implemented. Ez a
> dokumentum egy **determinisztikus deploy-scriptet** tervez, ami egy statikus
> bundle-t (jellemzoen az `ai-os startup` kimenetet, 20. doc) egy izolalt Docker
> nginx-kontenerbe rakva elove tesz egy subdomainen, a MEGLEVO Cloudflare Tunnel
> + host nginx infrastruktura mogott.
>
> ⚠️ **Ez a modul kozvetlenul erinti Norbert ELO, MEGOSZTOTT otthoni szerveret**,
> ami `ujjweb.hu`-t es sok mas valos projektet/ugyfel-oldalt szolgal ki EGYETLEN
> Cloudflare Tunnel + host nginx mogott. A dokumentum kozponti tervezesi elve
> ezert: **additiv-only, izolalt, idempotens, visszavonhato, es soha nem nyul
> megosztott konfig-blokkokhoz.** Lasd a globalis `~/.claude/CLAUDE.md` szabalyait
> es a `homelab-shared-server-context` / `feedback-cautious-shared-infra` /
> `server-freeze-investigation` memoriakat.

---

## 0. ⚠️ The Shared Server — Non-negotiable Rules

Mielott barmi masrol szo esne, ezek a kobe vesett korlatok. A script tervenek
minden dontese ezekbol kovetkezik:

1. **SOHA ne szerkessz meglevo, megosztott konfigot.** Se meglevo nginx
   `server {}` blokkot, se a `cloudflared` ingress-listat, se a crontabot, se a
   halozati configot. Kizarolag **uj, egyedi nevu fajlokat adunk hozza**
   (`/etc/nginx/conf.d/ai-os-startup-<sub>.conf`), amiket kesobb nyom nelkul el
   lehet tavolitani.
2. **Minden deploy izolalt.** Sajat Docker-kontener, sajat port, sajat nginx
   site-fajl, sajat registry-bejegyzes. Egy deploy bukasa/torlese egy masikat
   soha nem erinthet.
3. **Namespacing.** Minden AI-OS altal letrehozott objektum
   `ai-os-startup-<subdomain>` nevmintat visel (kontener, site-fajl, halozat),
   pontosan mint a `container_runner`/`cleaner` sajat nevterei — igy a
   `ai-os clean`-hez hasonlo, biztonsagos szelektiv takaritas lehetseges.
4. **A kontener NEM publikus kozvetlenul.** A port csak `127.0.0.1`-re publikalt
   (`-p 127.0.0.1:<port>:80`) — kizarolag a host reverse proxyn at erheto el,
   nem a nyilt internetrol, es nem a LAN felol.
5. **Validalj, aztan reload — SOHA restart.** Az nginx-ujratoltes elott kotelezo
   `nginx -t` (config-teszt); ha az hibazik, a script **visszavonja** az uj
   site-fajlt es nem tolt ujra. Reload (`nginx -s reload` / `systemctl reload`),
   nem restart — a restart az egesz hostot (minden mas projektet) leszakitana.
   Ugyanez a `cloudflared`-re, ha egyaltalan hozza kell nyulni (lasd wildcard).
6. **Root nelkul a kontenerben, read-only mount, eroforras-limit.** A statikus
   fajlok `:ro` mountolva; a kontener nem-root; `--memory`/`--cpus` korlat; nincs
   hozzaferese a host belso szolgaltatasaihoz.
7. **Dry-run alapbol az elso futasra + megerosites.** A script `--dry-run`-nal
   megmutatja pontosan MIT csinalna (milyen fajlt ir, milyen kontenert indit,
   reload-ol-e), mielott barmit tenne. Eles muvelet emberi megerositeshez kotott.
8. **Az AI soha nem futtatja ezt kozvetlenul.** Az `ai-os startup` (vagy a
   user) hivja meg a scriptet parameterrel; az LLM nem ad ki docker/nginx/
   cloudflared parancsot (kontextus-koltseg ES biztonsag — egy determinisztikus,
   auditalhato script sokkal vedhetobb egy live szerveren, mint egy AI ad-hoc
   parancsai).

---

## 1. Why a Script, Not an AI Tool?

- **Kontextus-koltseg.** A deploy sok apro, olvasas-nehez step (port kereses,
  config-iras, health-check, registry). Ha ezt az AI tool-hivasokkal csinalna,
  rengeteg tokent es tool-round-tripet enne — feleslegesen, hisz a stepek
  **determinisztikusak**.
- **Determinizmus & auditalhatosag.** Egy verziozott shell-script pontosan
  ugyanazt csinalja minden futaskor; git-diffelheto, reviewelheto, tesztelheto.
  Egy AI improvizalt parancs-sorozata nem.
- **Biztonsag a live hoston.** A legfontosabb ok. A megosztott szerveren egy
  rossz `nginx` vagy `docker` parancs sok valos oldalt vihet le. A script
  beepitett vedokorlatai (validalas, additiv-only, dry-run, rollback) garantaljak,
  hogy ez ne tortenhessen meg — egy szabad AI-parancs nem.

---

## 2. Request Architecture & Flow

```
[Bongeszo: https://freshbox.apps.ujjweb.hu]
        │  (TLS az edge-en)
        ▼
[Cloudflare edge]  ── proxied DNS (wildcard *.apps.ujjweb.hu → a tunnel) ──►
        │
        ▼
[cloudflared  (a MEGLEVO tunnel, valtozatlanul)]
        │  ingress: hostname *.apps.ujjweb.hu → http://localhost:80 (host nginx)
        ▼
[HOST nginx  (:80/:443, a MEGLEVO reverse proxy)]
        │  UJ additiv site-fajl: server_name freshbox.apps.ujjweb.hu;
        │  location / { proxy_pass http://127.0.0.1:39001; }
        ▼
[Docker nginx kontener  ai-os-startup-freshbox  → 127.0.0.1:39001]
        │  a statikus bundle :ro mountolva /usr/share/nginx/html-be
        ▼
[a generalt statikus site]
```

A kulcs a **wildcard** (2.1): egy egyszeri beallitas utan **minden uj deploy
tisztan additiv es csak a host nginx conf.d-jet erinti** — se DNS-, se
cloudflared-, se megosztott-blokk-valtozas deploykor.

### 2.1. Egyszeri beallitas (one-time, emberi review-val)

Ezt **egyszer** kell megcsinalni, tudatosan, a megosztott infra ismereteben
(nem a script automatikusan, mert megosztott config):

1. **Dedikalt wildcard zona.** Valassz egy AI-OS-nek fenntartott aldomaint, pl.
   `*.apps.ujjweb.hu` (vagy `*.startups.ujjweb.hu`). Igy az AI-OS deployok SOHA
   nem utkoznek a meglevo, kezzel kezelt subdomainekkel (`chatchat`, `wrenchly`
   stb.) — kulon nevterben elnek.
2. **Cloudflare DNS.** Egy proxied wildcard `CNAME *.apps → <tunnel-uuid>.
   cfargotunnel.com` (vagy a tunnel route-ja). Egyszeri.
3. **cloudflared ingress.** EGY additiv ingress-szabaly a meglevo tunnel
   configban: `hostname: "*.apps.ujjweb.hu" → service: http://localhost:80` (a
   host nginx). Ez az EGYETLEN cloudflared-erintes, es egyszeri — utana minden
   subdomain a host nginxen dol el a `Host` fejlec based on. (A meglevo ingress-
   szabalyokat nem bantjuk; a catch-all `404`/`http_status` szabaly ele, additiv
   modon szurjuk be.)
4. **Fenntartott nginx include-konyvtar + port-tartomany.** A
   `/etc/nginx/conf.d/ai-os-startup-*.conf` mintat az AI-OS deployok kizarolagos
   hasznalatara tartjuk fenn, es egy **port-tartomanyt** (pl. `39000–39999`) is,
   amit mas projekt nem hasznal.

Ezt a stept a script **nem** vegzi el magatol — legfeljebb egy kulon `deploy_
setup_check.sh` ellenorzi, hogy a wildcard + tartomany rendben van-e, es
figyelmeztet, ha nincs. Az elo szerveren a megosztott config elso beallitasa
mindig tudatos, reviewelt emberi muvelet (a globalis CLAUDE.md according to:
„investigate what else depends on it first and prefer additive changes").

### 2.2. Alternativa (NEM ajanlott alapbol): per-subdomain cloudflared ingress

Wildcard nelkul minden deploy egy-egy uj `cloudflared` ingress-szabalyt igenyelne
a megosztott tunnel-configban — ez **minden deploykor a megosztott configot
szerkesztene**, ami serti a 0. szabalyokat. Ezert az **A) wildcard ut az ajanlott**;
a per-subdomain ingress csak akkor jon szoba, ha wildcard valamiert nem opcio, es
akkor is szigoruan additiv, validalt (`cloudflared ... validate`), reload-olt (nem
restart) modon, dokumentalt rollbackkal.

---

## 3. The Deploy Script — `scripts/deploy_static.sh`

### 3.1. Parameterek

```
deploy_static.sh --subdomain <nev> --dir <statikus-bundle-utvonal>
                 [--zone apps.ujjweb.hu]     # a wildcard zona (default konfigbol)
                 [--port <n>]                # kezi port; alapbol auto a tartomanybol
                 [--dry-run]                 # csak mutatja, mit tenne — semmit nem hajt vegre
                 [--teardown]                # a subdomain deploy TELJES, additiv-only lebontasa
                 [--yes]                     # a megerosito kerdes atugrasa (CI-hez)
deploy_static.sh --list                      # az AI-OS deployok listaja (registry)
```

### 3.2. Stepek (eles futas)

1. **Bemenet-validalas.**
   - `subdomain`: szigoru regex (`^[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?$`), kisbetu,
     nincs pont (egy szintu a zonan belul).
   - **Foglalt-nev blocklist:** meglevo, kezzel kezelt hostok/aldomainek nevei
     (pl. `www`, `chatchat`, `wrenchly`, `portainer`, `jellyfin`, …) tiltottak,
     hogy veletlenul se lehessen egy eles szolgaltatas nevet „elfoglalni".
   - **Utkozes-ellenorzes:** ha mar van `ai-os-startup-<sub>` kontener/site, az
     **update** (idempotens ujradeploy), nem hiba.
   - `dir`: letezik, tartalmaz `index.html`-t, csak statikus fajlok (nincs
     szerveroldali kod, ami felrevezetne — statikus serve).
2. **Port-allokacio (determinisztikus).** A `39000–39999` tartomanybol: ha a
   subdomain mar a registryben van, az o portjat hasznalja (idempotencia);
   kulonben a legkisebb szabad portot foglalja (a registry + `ss -ltn` based on).
   Atomikus registry-iras (tempfile + `os.replace`, mint a `registry.py`).
3. **Kontener (izolalt, hardened).**
   ```
   docker run -d --name ai-os-startup-<sub> \
     -v <dir-abszolut>:/usr/share/nginx/html:ro \
     -p 127.0.0.1:<port>:80 \
     --restart unless-stopped \
     --memory=128m --cpus=0.5 \
     --cap-drop=ALL --security-opt no-new-privileges \
     --read-only --tmpfs /var/cache/nginx --tmpfs /var/run \
     -v <security-headers.conf>:/etc/nginx/conf.d/default.conf:ro \
     nginx:alpine
   ```
   - Csak `127.0.0.1`-re publikalt port → nem erheto el a proxyt kikerulve.
   - `:ro` mount, `--read-only` gyoker, `--cap-drop=ALL`, non-root — a statikus
     kiszolgalashoz semmi tobb nem kell.
   - **A kontener nginx configja biztonsagi fejleceket ad** (a site publikus a
     tunnelon at): `Content-Security-Policy` (a self-contained bundle-hoz szabva),
     `X-Frame-Options: DENY` (vagy SAMEORIGIN), `X-Content-Type-Options: nosniff`,
     `Referrer-Policy`. A gzip/cache-header a statikus assetekre.
   - Ez egy **perzisztens serve-kontener** (`--restart unless-stopped`), NEM az
     ephemeral validacios sandbox — mas eletciklus, mas cel. (A validacio a 20.
     doc according to mar lefutott a bundle-on, mielott ideer.)
4. **Additiv host-nginx site.** Uj fajl:
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
   (TLS-t az edge intezi Cloudflare-nel; a host↔kontener origin-forgalom a tunnelon
   belul http.)
5. **Validal, aztan reload (SOHA restart).**
   - `nginx -t` → ha **hibazik**, a script **torli az iment irt site-fajlt**,
     leallitja az uj kontenert, es hibaval kilep — a host valtozatlan marad.
   - ha rendben: `nginx -s reload` (a futo workerek grace-full ujratoltese — a
     tobbi oldal forgalma nem szakad meg).
6. **Health-check.** `curl -sf -H "Host: <sub>.apps.ujjweb.hu" http://127.0.0.1:80/`
   → 200-at kell adnia. Sikertelenseg in case of rollback (4–5. visszavonasa).
7. **Registry-bejegyzes.** `~/.ai-os/deploys.json` (az `AI_OS_HOME` ala, mint a
   `projects.json`): `{subdomain, port, container, site_file, dir, url, created_at}`
   — a `--list` es a `--teardown` innen dolgozik.
8. **Kiirja az elo URL-t:** `https://<sub>.apps.ujjweb.hu`.

### 3.3. Idempotencia es rollback

- **Ujradeploy** ugyanarra a subdomainre: a portot ujrahasznalja, a bundle-t
  atomikusan csereli (a mount ugyanaz a konyvtar; uj tartalom = a fajlok csereje,
  a kontener ujrainditasa nem is kell, mert a mount elo — vagy egy gyors
  `docker restart` a namespaced kontenerre). A site-fajl valtozatlan.
- **Rollback minden stepre.** Ha barmelyik step bukik, a script visszacsinalja
  az addigiakat (site-fajl torles, kontener stop/rm, registry-visszaallitas) —
  ugy, hogy a **host mindig konzisztens** maradjon. A kritikus invarians: **rossz
  nginx-config sosem kerul reload-ra** (a `nginx -t` kapu miatt), tehat egy hibas
  deploy nem viheti le a tobbi oldalt.

### 3.4. Teardown / list

- **`--teardown --subdomain <sub>`**: kizarolag a namespaced objektumokat bontja
  le — `docker rm -f ai-os-startup-<sub>`, az `ai-os-startup-<sub>.conf` torlese,
  `nginx -t` + reload, registry-bejegyzes torlese. Megosztott confighoz **nem
  nyul**. (A wildcard DNS/cloudflared egyszeri beallitas megmarad — az kozos
  infra, nem per-deploy.)
- **`--list`**: a registrybol kiirja az elo AI-OS deployokat (subdomain, url,
  port, kontener, kor).
- **`ai-os clean` integracio:** a `cleaner.py` bovitheto ugy, hogy `--deploys`
  kapcsoloval a registry based on listazza/takaritsa az elarvult
  `ai-os-startup-*` kontenereket + site-fajlokat (ugyanaz az „csak a sajat
  nevterunk" elv, mint a sandbox-artefaktoknal).

---

## 4. Port Allocation and Registry

- **Fenntartott tartomany:** `39000–39999` (konfiguralhato), amit mas projekt nem
  hasznal a hoston. A tartomany fixalasa egyszeri, dokumentalt dontes.
- **Registry:** `~/.ai-os/deploys.json`, atomikus iras (tempfile + `os.replace`),
  a `registry.py` mintajara. Ez a single-source-of-truth a port↔subdomain
  lekepezesrol, a teardownrol es a listazasrol.
- **Utkozes-vedelem:** foglalaskor a script a registry ES az elo portok
  (`ss -ltn`) uniojat nezi, hogy soha ne foglaljon elo portot.

---

## 5. Security Checklist (For Every Production Run)

A script inditaskor ellenorzi es a `--dry-run` kiirja:

- [ ] a subdomain a fenntartott **wildcard zonaban** van (nem a gyoker-domainen,
      nem egy kezzel kezelt subdomainen)
- [ ] a subdomain **nincs a foglalt-nev blocklistan**
- [ ] a port a **fenntartott tartomanyban** van es szabad
- [ ] a kontener portja **`127.0.0.1`-re** publikalt (nem `0.0.0.0`)
- [ ] a mount **`:ro`**, a kontener **non-root**, van eroforras-limit
- [ ] **csak uj, namespaced** nginx site-fajl keszul; meglevo blokk nem modosul
- [ ] **`nginx -t` sikeres** a reload ELOTT; hiba → rollback, nincs reload
- [ ] **reload, nem restart**
- [ ] a muvelet a registrybe kerul (visszavonhatosag)

Amit a script **SOHA nem tehet**: meglevo nginx/cloudflared blokk szerkesztese,
nginx/cloudflared **restart**, `0.0.0.0`-ra publikalas, root kontener, a
fenntartott zonan/tartomanyon kivuli objektum letrehozasa, a megosztott tunnel-
config per-deploy modositasa (wildcard mellett nincs is ra szukseg).

---

## 6. Integration with `ai-os startup`

Az `ai-os startup --subdomain <nev>` a sikeres validacio utan **egyetlen script-
hivast** tesz: `deploy_static.sh --subdomain <nev> --dir <bundle>`. Az AI ezen a
ponton kilep a kepbol — nem lat, nem futtat infra-parancsot. A script kimenete
(az elo URL vagy a hiba) visszakerul a CLI-be. Ez a responsible forseg-elvalasztas a
lenyeg: **az AI statikus fajlokat gyart; a determinisztikus, hardened script
deployol; az ember (HITL preview) engedelyez.**

---

## 7. Security Rationale for Shared Server

| Kockazat | Hogyan zarjuk ki |
| -------- | ---------------- |
| Egy deploy leviszi a tobbi oldalt | `nginx -t` a reload elott + reload (nem restart) + rollback |
| Megosztott config elrontasa | additiv-only, csak uj namespaced fajlok, wildcard = 0 per-deploy kozos-config-erintes |
| Port/subdomain utkozes elo szolgaltatassal | fenntartott zona + tartomany + blocklist + registry |
| Kontener-kitores / host-hozzaferes | non-root, `--cap-drop=ALL`, read-only, `:ro` mount, 127.0.0.1-port |
| Nyilvanos kozvetlen eleres a proxyt kikerulve | a port csak `127.0.0.1`-en publikalt |
| Elarvult eroforrasok crash utan | registry + `--teardown` + `ai-os clean --deploys` (namespaced) |
| AI ad-hoc infra-parancsai | az AI nem futtat infra-parancsot; csak a script teszi |

---

## 8. Future Extensions

- **TTL / auto-lejarat.** A demo-subdomainek automatikus lebontasa N nap utan a
  registry `created_at`-ja based on (egy `deploy_static.sh --gc` a crontabban —
  additiv, namespaced).
- **Kvota.** Max. parhuzamos AI-OS deploy szam (a fenntartott tartomany merete
  amugy is termeszetes felso korlat).
- **Alap-auth / „nem indexelendo".** Egy egyszeru Cloudflare Access szabaly vagy
  `X-Robots-Tag: noindex` a demokra, hogy ne szivarogjanak ki ido elott.
- **Statikus TLS-origin.** Ha valaha kozvetlen (tunnel nelkuli) eleres kellene,
  Cloudflare Origin Cert — de a jelenlegi tunnel-modell ezt feleslegesse teszi.
- **Blue/green demo-frissites.** Uj bundle egy masodik namespaced kontenerbe,
  atomikus site-fajl-atkapcsolas, majd a regi lebontasa — nulla-allasidos
  frissites (opcionalis, a demokhoz altalaban felesleges).

---

## Related Documents

- `20_STARTUP_GENERATOR.md` — az `ai-os startup`, ami ezt a scriptet meghivja.
- `11_ORCHESTRATOR_TECH_STACK_AND_DEPL.md` — az AI-OS sajat deploy/tech-stack
  megfontolasai.
- Globalis `~/.claude/CLAUDE.md` + a `homelab-shared-server-context` /
  `feedback-cautious-shared-infra` / `server-freeze-investigation` memoriak — a
  megosztott szerver konkret incidensei es megerositett mintai, amikbol a 0.
  szabalyok szarmaznak.
```
