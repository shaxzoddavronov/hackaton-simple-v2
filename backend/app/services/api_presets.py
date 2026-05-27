"""Hard-coded endpoint catalogs for popular CRM / ERP / 1C platforms.

A "preset" is a list of :class:`ParsedEndpoint` we ship in the codebase
so users can connect to a vendor without uploading the vendor's
OpenAPI spec. The agent uses these the same way it uses parsed specs:
they become rows in the workspace's schema bundle, and the planner
picks among them when answering questions.

Each preset captures the bare minimum the planner needs:
  * the endpoint path,
  * the most common query parameters (limit, offset, filter shorthands),
  * the response's top-level row shape (so chart_designer has columns).

We intentionally include only the **list / collection** endpoints per
entity — read-only analytics rarely needs the per-id detail endpoints,
and including everything bloats the planner prompt.

References (these are stable public surfaces):
  * Bitrix24 REST: https://apidocs.bitrix24.com/api-reference/crm/
  * AmoCRM v4: https://www.amocrm.ru/developers/content/crm_platform/api-reference
  * 1C:Enterprise OData: standard /odata/standard.odata/Catalog_* shape.
  * HubSpot CRM v3: https://developers.hubspot.com/docs/api/crm
  * Salesforce REST: /services/data/vXX/sobjects.

Adding a new preset is a single dict entry — no engine changes needed.
"""
from __future__ import annotations

from app.services.openapi_parser import ParsedEndpoint, ParsedField, ParsedParam


def _ep(
    path: str,
    summary: str,
    *,
    params: list[ParsedParam] | None = None,
    response_fields: list[ParsedField] | None = None,
) -> ParsedEndpoint:
    return ParsedEndpoint(
        method="GET",
        path=path,
        summary=summary,
        params=params or [],
        response_fields=response_fields or [],
    )


def _p(name: str, type_: str = "string", *, required: bool = False) -> ParsedParam:
    return ParsedParam(name=name, location="query", type=type_, required=required)


def _f(name: str, type_: str = "string") -> ParsedField:
    return ParsedField(name=name, type=type_)


# ── Bitrix24 (REST webhook-style) ──────────────────────────────────
# URL pattern: ``{base_url}/rest/{method}.list?auth=...`` — base_url
# carries the per-portal webhook prefix, so paths here start at /rest/.

_BITRIX24: list[ParsedEndpoint] = [
    _ep(
        "/rest/crm.contact.list",
        "List CRM contacts (paginated)",
        params=[
            _p("start", "integer"),
            _p("filter[NAME]"),
            _p("filter[EMAIL]"),
            _p("select[]"),
            _p("order[ID]"),
        ],
        response_fields=[
            _f("ID", "integer"), _f("NAME"), _f("LAST_NAME"),
            _f("EMAIL", "array"), _f("PHONE", "array"), _f("COMPANY_ID", "integer"),
            _f("DATE_CREATE", "timestamp"), _f("ASSIGNED_BY_ID", "integer"),
        ],
    ),
    _ep(
        "/rest/crm.deal.list",
        "List CRM deals",
        params=[
            _p("start", "integer"),
            _p("filter[STAGE_ID]"),
            _p("filter[CATEGORY_ID]", "integer"),
            _p("select[]"),
            _p("order[OPPORTUNITY]"),
        ],
        response_fields=[
            _f("ID", "integer"), _f("TITLE"), _f("STAGE_ID"),
            _f("OPPORTUNITY", "number"), _f("CURRENCY_ID"),
            _f("CONTACT_ID", "integer"), _f("COMPANY_ID", "integer"),
            _f("ASSIGNED_BY_ID", "integer"), _f("DATE_CREATE", "timestamp"),
            _f("CLOSEDATE", "timestamp"),
        ],
    ),
    _ep(
        "/rest/crm.lead.list",
        "List CRM leads",
        params=[
            _p("start", "integer"),
            _p("filter[STATUS_ID]"),
            _p("filter[SOURCE_ID]"),
        ],
        response_fields=[
            _f("ID", "integer"), _f("TITLE"), _f("NAME"), _f("LAST_NAME"),
            _f("STATUS_ID"), _f("SOURCE_ID"),
            _f("OPPORTUNITY", "number"), _f("CURRENCY_ID"),
            _f("DATE_CREATE", "timestamp"),
        ],
    ),
    _ep(
        "/rest/crm.company.list",
        "List CRM companies",
        params=[
            _p("start", "integer"),
            _p("filter[INDUSTRY]"),
            _p("filter[COMPANY_TYPE]"),
        ],
        response_fields=[
            _f("ID", "integer"), _f("TITLE"), _f("COMPANY_TYPE"),
            _f("INDUSTRY"), _f("REVENUE", "number"), _f("EMPLOYEES", "integer"),
            _f("DATE_CREATE", "timestamp"),
        ],
    ),
    _ep(
        "/rest/tasks.task.list",
        "List tasks (filterable)",
        params=[
            _p("filter[STATUS]"),
            _p("filter[RESPONSIBLE_ID]", "integer"),
            _p("start", "integer"),
        ],
        response_fields=[
            _f("ID", "integer"), _f("TITLE"), _f("STATUS"),
            _f("RESPONSIBLE_ID", "integer"), _f("CREATED_DATE", "timestamp"),
            _f("DEADLINE", "timestamp"), _f("PRIORITY"),
        ],
    ),
]


# ── AmoCRM v4 ──────────────────────────────────────────────────────

_AMOCRM: list[ParsedEndpoint] = [
    _ep(
        "/api/v4/leads",
        "List leads",
        params=[
            _p("page", "integer"), _p("limit", "integer"),
            _p("filter[statuses][0][pipeline_id]", "integer"),
            _p("filter[statuses][0][status_id]", "integer"),
            _p("filter[created_at][from]", "integer"),
            _p("filter[created_at][to]", "integer"),
        ],
        response_fields=[
            _f("id", "integer"), _f("name"), _f("price", "number"),
            _f("status_id", "integer"), _f("pipeline_id", "integer"),
            _f("responsible_user_id", "integer"),
            _f("created_at", "integer"), _f("updated_at", "integer"),
            _f("closed_at", "integer"),
        ],
    ),
    _ep(
        "/api/v4/contacts",
        "List contacts",
        params=[
            _p("page", "integer"), _p("limit", "integer"),
            _p("query"),
        ],
        response_fields=[
            _f("id", "integer"), _f("name"), _f("first_name"), _f("last_name"),
            _f("responsible_user_id", "integer"),
            _f("created_at", "integer"), _f("updated_at", "integer"),
        ],
    ),
    _ep(
        "/api/v4/companies",
        "List companies",
        params=[_p("page", "integer"), _p("limit", "integer")],
        response_fields=[
            _f("id", "integer"), _f("name"),
            _f("responsible_user_id", "integer"),
            _f("created_at", "integer"), _f("updated_at", "integer"),
        ],
    ),
    _ep(
        "/api/v4/users",
        "List portal users",
        params=[_p("page", "integer"), _p("limit", "integer")],
        response_fields=[
            _f("id", "integer"), _f("name"), _f("email"),
            _f("role_id", "integer"), _f("group_id", "integer"),
        ],
    ),
]


# ── 1C:Enterprise OData ─────────────────────────────────────────────
# Standard OData v3 surface that every 1C database exposes by default
# under ``/odata/standard.odata/``. Entity names follow 1C's bilingual
# naming — we transliterate to ASCII so the planner can write them
# without dealing with UTF-8 issues, but the actual 1C endpoint expects
# Cyrillic; users may have to URL-encode the path on the wire. (We
# document the most common entities with their canonical English
# transliterations.)

_ODATA_1C: list[ParsedEndpoint] = [
    _ep(
        "/odata/standard.odata/Catalog_Counterparties",
        "List counterparties (контрагенты)",
        params=[
            _p("$top", "integer"), _p("$skip", "integer"),
            _p("$filter"), _p("$select"), _p("$orderby"),
            _p("$format"),
        ],
        response_fields=[
            _f("Ref_Key"), _f("Code"), _f("Description"),
            _f("INN"), _f("KPP"), _f("DeletionMark", "boolean"),
        ],
    ),
    _ep(
        "/odata/standard.odata/Catalog_Products",
        "List products (номенклатура)",
        params=[
            _p("$top", "integer"), _p("$skip", "integer"),
            _p("$filter"), _p("$format"),
        ],
        response_fields=[
            _f("Ref_Key"), _f("Code"), _f("Description"),
            _f("Article"), _f("UnitOfMeasure_Key"),
            _f("DeletionMark", "boolean"),
        ],
    ),
    _ep(
        "/odata/standard.odata/Document_Sales",
        "List sales documents (реализация товаров и услуг)",
        params=[
            _p("$top", "integer"), _p("$filter"),
            _p("$format"),
        ],
        response_fields=[
            _f("Ref_Key"), _f("Number"), _f("Date", "timestamp"),
            _f("Counterparty_Key"), _f("DocumentAmount", "number"),
            _f("Posted", "boolean"), _f("Organization_Key"),
        ],
    ),
    _ep(
        "/odata/standard.odata/Document_Purchases",
        "List purchase documents (поступление товаров и услуг)",
        params=[
            _p("$top", "integer"), _p("$filter"),
            _p("$format"),
        ],
        response_fields=[
            _f("Ref_Key"), _f("Number"), _f("Date", "timestamp"),
            _f("Counterparty_Key"), _f("DocumentAmount", "number"),
            _f("Posted", "boolean"),
        ],
    ),
    _ep(
        "/odata/standard.odata/AccumulationRegister_GoodsInWarehouses",
        "Warehouse stock (товары на складах)",
        params=[_p("$filter"), _p("$format")],
        response_fields=[
            _f("Period", "timestamp"), _f("Warehouse_Key"),
            _f("Product_Key"), _f("Quantity", "number"),
        ],
    ),
]


# ── HubSpot CRM v3 ─────────────────────────────────────────────────

_HUBSPOT: list[ParsedEndpoint] = [
    _ep(
        "/crm/v3/objects/contacts",
        "List contacts",
        params=[
            _p("limit", "integer"), _p("after"),
            _p("properties"), _p("archived", "boolean"),
        ],
        response_fields=[
            _f("id"), _f("createdAt", "timestamp"), _f("updatedAt", "timestamp"),
            _f("properties", "object"),
        ],
    ),
    _ep(
        "/crm/v3/objects/companies",
        "List companies",
        params=[_p("limit", "integer"), _p("after"), _p("properties")],
        response_fields=[
            _f("id"), _f("createdAt", "timestamp"), _f("updatedAt", "timestamp"),
            _f("properties", "object"),
        ],
    ),
    _ep(
        "/crm/v3/objects/deals",
        "List deals",
        params=[_p("limit", "integer"), _p("after"), _p("properties")],
        response_fields=[
            _f("id"), _f("createdAt", "timestamp"), _f("updatedAt", "timestamp"),
            _f("properties", "object"),
        ],
    ),
    _ep(
        "/crm/v3/objects/tickets",
        "List tickets",
        params=[_p("limit", "integer"), _p("after"), _p("properties")],
        response_fields=[
            _f("id"), _f("createdAt", "timestamp"), _f("updatedAt", "timestamp"),
            _f("properties", "object"),
        ],
    ),
]


# ── Salesforce REST v59 ────────────────────────────────────────────

_SALESFORCE: list[ParsedEndpoint] = [
    _ep(
        "/services/data/v59.0/sobjects/Account",
        "Describe Account sobject",
        params=[],
        response_fields=[
            _f("Id"), _f("Name"), _f("Industry"),
            _f("AnnualRevenue", "number"), _f("Type"),
            _f("CreatedDate", "timestamp"),
        ],
    ),
    _ep(
        "/services/data/v59.0/sobjects/Contact",
        "Describe Contact sobject",
        params=[],
        response_fields=[
            _f("Id"), _f("FirstName"), _f("LastName"),
            _f("Email"), _f("Phone"), _f("AccountId"),
            _f("CreatedDate", "timestamp"),
        ],
    ),
    _ep(
        "/services/data/v59.0/sobjects/Opportunity",
        "Describe Opportunity sobject",
        params=[],
        response_fields=[
            _f("Id"), _f("Name"), _f("StageName"),
            _f("Amount", "number"), _f("CloseDate", "timestamp"),
            _f("AccountId"), _f("OwnerId"),
        ],
    ),
    _ep(
        "/services/data/v59.0/sobjects/Lead",
        "Describe Lead sobject",
        params=[],
        response_fields=[
            _f("Id"), _f("FirstName"), _f("LastName"), _f("Company"),
            _f("Status"), _f("Email"), _f("CreatedDate", "timestamp"),
        ],
    ),
    _ep(
        "/services/data/v59.0/query",
        "SOQL query passthrough (q= parameter)",
        params=[_p("q", "string", required=True)],
        response_fields=[
            _f("totalSize", "integer"), _f("done", "boolean"),
            _f("records", "array"),
        ],
    ),
]


PRESETS: dict[str, list[ParsedEndpoint]] = {
    "bitrix24": _BITRIX24,
    "amocrm": _AMOCRM,
    "odata_1c": _ODATA_1C,
    "hubspot": _HUBSPOT,
    "salesforce": _SALESFORCE,
    "generic": [],
}


def load_preset(name: str) -> list[ParsedEndpoint]:
    if name not in PRESETS:
        raise ValueError(
            f"Unknown REST API preset: {name!r}. "
            f"Known: {sorted(PRESETS)}"
        )
    # Return a copy so callers can't mutate the canonical catalog.
    return list(PRESETS[name])
