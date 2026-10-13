# ThousandEyes read-only API

Petit service FastAPI qui expose, **sans authentification**, quelques endpoints
en lecture au-dessus de l'API ThousandEyes v7 :

| Endpoint | Rôle | API ThousandEyes appelée |
|---|---|---|
| `GET /alerts` | liste des alertes | `GET /v7/alerts` |
| `GET /alerts/{alert_id}` | détail d'une alerte, **joint à la config de son test** | `GET /v7/alerts/{id}`, puis catalogue + détail du test |
| `GET /tests` | catalogue des tests (pour trouver un `testId`) | `GET /v7/tests` + `GET /v7/endpoint/tests/scheduled-tests` |
| `GET /tests/{test_id}` | configuration détaillée d'un test | catalogue, puis `GET /v7/tests/{type}/{test_id}` |
| `GET /health` | sonde de vivacité | — (aucun appel) |

Le token ThousandEyes reste côté serveur : les clients (agent, n8n, script,
dashboard) appellent ce service en HTTP nu.

## 1. Récupérer un token ThousandEyes

Dans l'interface : **Account Settings → Users and Roles → User API Tokens →
OAuth Bearer Token**. Les droits en lecture sur *Alerts* et *Tests* suffisent.
Le token est un bearer : `Authorization: Bearer <token>`.

Vérification directe :

```bash
curl -s -H "Authorization: Bearer $TE_TOKEN" https://api.thousandeyes.com/v7/alerts?window=12h | jq
```

Si le compte a plusieurs *account groups*, récupérez l'`aid` voulu :

```bash
curl -s -H "Authorization: Bearer $TE_TOKEN" https://api.thousandeyes.com/v7/account-groups | jq '.accountGroups[] | {aid, accountGroupName}'
```

## 2. Configurer

```bash
cp .env.example .env
```

Puis renseignez `TE_TOKEN` (et `TE_AID` si vous visez un groupe de compte
précis). Le service refuse de démarrer sans `TE_TOKEN`.

## 3. Lancer

```bash
uv run uvicorn main:app --reload --port 8000
```

Documentation interactive : <http://127.0.0.1:8000/docs>.

## 4. Utiliser

Alertes des 12 dernières heures (défaut) :

```bash
curl -s localhost:8000/alerts | jq
```

Alertes actives de la dernière heure :

```bash
curl -s "localhost:8000/alerts?window=1h&state=trigger" | jq
```

Plage explicite (UTC, ISO 8601 — remplace `window`) :

```bash
curl -s "localhost:8000/alerts?from_date=2026-10-13T06:00:00Z&to_date=2026-10-13T12:00:00Z" | jq
```

Paramètres : `window` (`30m`, `12h`, `7d`…), `state` (`trigger`/`clear`),
`from_date`/`to_date`, `max_results`, `aid`.

Catalogue des tests (champ `unavailable` = catalogues refusés par le token) :

```bash
curl -s localhost:8000/tests | jq '.tests[] | {testId, testName, type}'
```

### Détail d'une alerte, avec l'URL testée

Une alerte v7 ne porte **ni l'URL testée, ni le nom du test** : seulement un
lien `_links.test.href`. `GET /alerts/{alert_id}` fait la jonction côté serveur
et renvoie l'alerte augmentée d'un bloc `test` :

```bash
curl -s localhost:8000/alerts/$(curl -s localhost:8000/alerts | jq -r '.alerts[0].id') \
  | jq '{alertType, alertState, startDate, test: .test | del(.config), details}'
```

```json
{
  "alertType": "endpoint-http-server",
  "alertState": "clear",
  "startDate": "2026-10-12T05:46:00Z",
  "test": {
    "testId": "408730",
    "testName": "Slack - Tenant",
    "type": "http-server",
    "target": "https://xvalette.slack.com",
    "port": 443, "protocol": "autodetect", "interval": 60
  },
  "details": [
    { "name": "MacBook-Pro-de-xvalette", "start": { "metrics": "Error Type: \"HTTP\"" }, "state": "trigger" }
  ]
}
```

`test.target` est la cible réellement testée, `details[]` dit **qui** a
déclenché et **quelle métrique** a violé. La configuration complète du test
reste disponible sous `test.config`.

Détail d'un test seul, si on a déjà son `testId` :

```bash
curl -s localhost:8000/tests/1234567 | jq
```

## Notes d'implémentation

- **Pas de route générique `/v7/tests/{testId}`** dans l'API v7 : chaque type de
  test a son chemin (`/tests/agent-to-server/{id}`, `/tests/dns-server/{id}`…),
  et les tests portés par des **Endpoint Agents** vivent dans un arbre séparé
  (`/endpoint/tests/scheduled-tests/http-server/{id}`), absent de `GET /v7/tests`.
  Le service liste donc les **deux** catalogues, met en cache la correspondance
  `testId → _links.self.href` (TTL `TE_CACHE_TTL`, 60 s) et suit ce lien. Aucune
  table de types à maintenir.
- **Piège vérifié sur l'API réelle** : pour une alerte d'Endpoint Agent, le lien
  `_links.test.href` renvoyé par `/alerts` pointe vers
  `/v7/tests/endpoint-tests/{id}` — une route qui répond **404**. Suivre ce lien
  naïvement ne marche pas ; c'est pourquoi la résolution passe par le catalogue.
- **Cible normalisée** : le champ qui porte la cible dépend du type de test —
  `url` (http-server), `server` (agent-to-server), `domain` (dns-server,
  dns-trace), `targetAgentId` (agent-to-agent). `test.target` expose ces cas
  sous un nom unique.
- **Alerte sans test exploitable** : droits par catalogue, test supprimé, ou
  type d'alerte qui n'est pas lié à un test (`endpoint-browser-sessions-agent`
  porte un device, pas un test, et n'a aucun lien `test`). La réponse renvoie
  alors l'alerte avec `test: null` et la raison dans `testUnavailable`, au lieu
  d'échouer en bloc — même logique que le champ `unavailable` de `/tests`.
- **Erreurs** : un 401/403 de ThousandEyes est remonté en `502` avec un message
  explicite — c'est une erreur de configuration du service, pas du client. Les
  `404` et `429` sont transmis tels quels.
- **Droits par catalogue** : le scope Endpoint Agent est distinct de celui des
  tests planifiés. Un token qui n'a qu'un des deux reçoit `403` sur l'autre ; le
  service continue avec ce qui est accessible et le signale dans `unavailable`,
  au lieu d'échouer en bloc.
- Les champs `alertId`, `dateStart`, `state`, `severity` de la réponse v7 sont
  dépréciés ; utilisez `id`, `startDate`, `alertState`, `alertSeverity`.

## Exposition réseau

Le service n'a aucune authentification : ne l'exposez pas tel quel sur
Internet. Par défaut uvicorn n'écoute que sur `127.0.0.1`. Pour un accès
distant, passez par un tunnel ou un reverse proxy qui porte l'authentification
(c'est le rôle de Cloudflare Tunnel dans le lab
[your-first-ai-agent](https://github.com/xaviervalette/your-first-ai-agent)).
