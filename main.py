"""API HTTP (sans authentification) au-dessus de l'API ThousandEyes v7.

Deux endpoints :
  * GET /alerts          -> liste des alertes
  * GET /tests/{test_id} -> detail (configuration) d'un test

Le token ThousandEyes est lu dans l'environnement (TE_TOKEN) et ne sort jamais
de ce service : les clients appellent cette API sans aucune authentification.
"""

import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("te-api")


class Settings(BaseSettings):
    """Configuration lue depuis l'environnement ou un fichier .env."""

    model_config = SettingsConfigDict(env_prefix="TE_", env_file=".env", extra="ignore")

    token: str
    aid: str | None = None
    base_url: str = "https://api.thousandeyes.com/v7"
    timeout: float = 30.0
    cache_ttl: float = 60.0


settings = Settings()
BASE_URL = settings.base_url.rstrip("/")
ALLOWED_HOST = urlparse(BASE_URL).netloc

# Cache testId -> URL typee du detail du test, rafraichi par TTL.
_test_index: dict[str, str] = {}
_test_index_at: float = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Un seul client HTTP (connexions reutilisees) pour toute la duree du service."""
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=settings.timeout,
        headers={
            "Authorization": f"Bearer {settings.token}",
            "Accept": "application/hal+json",
        },
    ) as client:
        app.state.client = client
        yield


app = FastAPI(
    title="ThousandEyes read-only API",
    version="0.1.0",
    description="Expose les alertes ThousandEyes et le detail des tests, sans authentification.",
    lifespan=lifespan,
)


async def te_get(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    """Appelle l'API ThousandEyes et traduit ses erreurs en erreurs HTTP lisibles."""
    clean = {k: v for k, v in params.items() if v is not None}
    try:
        response = await client.get(url, params=clean)
    except httpx.HTTPError as exc:
        logger.warning("Appel ThousandEyes %s impossible: %s", url, exc)
        raise HTTPException(status_code=502, detail=f"ThousandEyes inatteignable: {exc}") from exc

    if response.is_success:
        return response.json()

    messages = {
        401: "Token ThousandEyes invalide ou expire (TE_TOKEN).",
        403: "Token ThousandEyes sans les droits necessaires sur cette ressource.",
        404: "Ressource introuvable cote ThousandEyes.",
        429: "Quota d'appels ThousandEyes epuise, reessayez plus tard.",
    }
    detail = messages.get(response.status_code, f"Erreur ThousandEyes: {response.text[:300]}")
    logger.warning("ThousandEyes %s -> HTTP %s", url, response.status_code)
    # 401/403 cote ThousandEyes = probleme de configuration de CE service, pas du
    # client : on renvoie 502 pour ne pas lui suggerer de s'authentifier.
    status = 502 if response.status_code in (401, 403) else response.status_code
    raise HTTPException(status_code=status, detail=detail)


async def test_url(client: httpx.AsyncClient, test_id: str, aid: str | None) -> str:
    """Resout l'URL de detail d'un test.

    L'API v7 n'a pas de route generique /tests/{testId} : chaque type de test a
    la sienne (/tests/agent-to-server/{id}, /tests/dns-server/{id}, ...), et les
    tests portes par des Endpoint Agents vivent dans un arbre separe
    (/endpoint/tests/scheduled-tests/http-server/{id}). On liste donc les deux
    catalogues et on suit le lien `_links.self` fourni par l'API, ce qui evite
    de maintenir une table des types en dur.

    A noter : le lien `_links.test.href` porte par les alertes d'Endpoint Agent
    pointe vers /tests/endpoint-tests/{id}, une route qui repond 404. C'est la
    raison pour laquelle on resout par le catalogue plutot qu'en suivant ce
    lien.
    """
    global _test_index, _test_index_at

    fresh = time.monotonic() - _test_index_at < settings.cache_ttl
    if not (fresh and test_id in _test_index):
        index: dict[str, str] = {}
        catalogs = ("/endpoint/tests/scheduled-tests", "/tests")
        failures: list[str] = []
        for catalog in catalogs:
            # Un token peut n'avoir les droits que sur un seul des deux
            # catalogues (le scope Endpoint Agent est distinct) : on continue
            # avec ce qui est accessible plutot que d'echouer en bloc.
            try:
                payload = await te_get(client, catalog, {"aid": aid})
            except HTTPException as exc:
                logger.warning("Catalogue %s inaccessible: %s", catalog, exc.detail)
                failures.append(catalog)
                continue
            index.update(
                {
                    str(t["testId"]): href
                    for t in payload.get("tests", [])
                    if t.get("testId") and (href := t.get("_links", {}).get("self", {}).get("href"))
                }
            )
        if len(failures) == len(catalogs):
            raise HTTPException(
                status_code=502,
                detail="Aucun catalogue de tests accessible avec ce token (droits Tests manquants).",
            )
        _test_index = index
        _test_index_at = time.monotonic()

    href = _test_index.get(test_id)
    if href is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Test {test_id} introuvable dans ce groupe de compte "
                "(ni dans les tests planifies, ni dans les tests Endpoint Agent)."
            ),
        )
    # Le lien vient de l'API ThousandEyes, mais on verifie l'hote avant de le
    # suivre avec le token dans les en-tetes.
    if urlparse(href).netloc != ALLOWED_HOST:
        raise HTTPException(status_code=502, detail="Lien de test inattendu renvoye par ThousandEyes.")
    return href


@app.get("/health", summary="Sonde de vivacite")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/alerts", summary="Liste les alertes ThousandEyes")
async def list_alerts(
    request: Request,
    window: Annotated[
        str | None,
        Query(
            description="Fenetre glissante se terminant maintenant, ex. 30m, 12h, 7d. Exclusif avec from_date/to_date.",
            pattern=r"^\d+[smhdw]$",
        ),
    ] = "12h",
    state: Annotated[
        Literal["trigger", "clear"] | None,
        Query(description="trigger = alertes actives, clear = alertes resorbees."),
    ] = None,
    from_date: Annotated[
        str | None, Query(description="Debut de plage UTC ISO 8601, ex. 2026-10-13T08:00:00Z.")
    ] = None,
    to_date: Annotated[str | None, Query(description="Fin de plage UTC ISO 8601.")] = None,
    max_results: Annotated[int | None, Query(ge=1, le=1000, description="Nombre maximum d'alertes.")] = None,
    aid: Annotated[str | None, Query(description="Account Group ID, surcharge TE_AID.")] = None,
) -> dict[str, Any]:
    """Proxy de `GET /v7/alerts`.

    Sans parametre de date, renvoie les alertes des 12 dernieres heures. Si
    from_date est fourni, il prend le pas sur window (l'API refuse les deux).
    """
    params: dict[str, Any] = {
        "state": state,
        "max": max_results,
        "aid": aid or settings.aid,
    }
    if from_date:
        params["startDate"] = from_date
        params["endDate"] = to_date
    else:
        params["window"] = window

    payload = await te_get(request.app.state.client, "/alerts", params)
    alerts = payload.get("alerts", [])
    return {"count": len(alerts), "alerts": alerts}


@app.get("/tests", summary="Liste les tests configures")
async def list_tests(
    request: Request,
    aid: Annotated[str | None, Query(description="Account Group ID, surcharge TE_AID.")] = None,
) -> dict[str, Any]:
    """Catalogue des tests, pour recuperer les testId a passer a /tests/{test_id}.

    Agrege les tests planifies (`/v7/tests`) et les tests Endpoint Agent
    (`/v7/endpoint/tests/scheduled-tests`), dont les droits sont distincts : un
    catalogue inaccessible est signale dans `unavailable` au lieu de faire
    echouer la reponse.
    """
    client = request.app.state.client
    effective_aid = aid or settings.aid
    tests: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for catalog in ("/endpoint/tests/scheduled-tests", "/tests"):
        try:
            payload = await te_get(client, catalog, {"aid": effective_aid})
        except HTTPException as exc:
            logger.warning("Catalogue %s inaccessible: %s", catalog, exc.detail)
            unavailable.append(catalog)
            continue
        for t in payload.get("tests", []):
            tests.append(
                {
                    "testId": t.get("testId"),
                    "testName": t.get("testName"),
                    "type": t.get("type") or t.get("testType"),
                    "catalog": catalog,
                    "detailUrl": t.get("_links", {}).get("self", {}).get("href"),
                }
            )
    return {"count": len(tests), "unavailable": unavailable, "tests": tests}


@app.get("/tests/{test_id}", summary="Detail de configuration d'un test")
async def get_test(
    request: Request,
    test_id: str,
    aid: Annotated[str | None, Query(description="Account Group ID, surcharge TE_AID.")] = None,
) -> dict[str, Any]:
    """Configuration complete d'un test : type, cible, agents, seuils, intervalle.

    Le testId se recupere dans `_links.test.href` des alertes renvoyees par
    `/alerts`.
    """
    client = request.app.state.client
    effective_aid = aid or settings.aid
    url = await test_url(client, test_id, effective_aid)
    return await te_get(client, url, {"aid": effective_aid, "expand": "agent"})
