"""Bounded read-only state fingerprints shared by source snapshot and isolated target."""

import re

from scripts.recovery_bundle import canonical, digest, require, verify_receipt

MAX_DB = 64 * 1024**2
MAX_TABLES = 128
MAX_ROWS = 100000
MAX_TOTAL_ROWS = 200000
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def rows(query, selection, order):
    return query(
        f"SELECT COALESCE(jsonb_agg(to_jsonb(x) ORDER BY {order}), '[]'::jsonb) FROM ({selection}) x"
    )


def role_state(query, exclude=""):
    require(exclude == "" or re.fullmatch(r"recovery_[0-9a-f]{32}", exclude) is not None)
    role_count = query("SELECT to_jsonb(count(*)) FROM (SELECT 1 FROM pg_authid LIMIT 129) x")
    member_count = query(
        "SELECT to_jsonb(count(*)) FROM (SELECT 1 FROM pg_auth_members LIMIT 257) x"
    )
    require(role_count <= 128 and member_count <= 256, "role metadata limit")
    roles = rows(
        query,
        "SELECT rolname, encode(sha256(convert_to((to_jsonb(r)-'oid')::text,'UTF8')),'hex') AS digest "
        f"FROM pg_authid r WHERE rolname !~ '^pg_' AND rolname <> '{exclude}'",
        "rolname",
    )
    members = rows(
        query,
        "SELECT r.rolname AS role, m.rolname AS member, g.rolname AS grantor, "
        "a.admin_option, a.inherit_option, a.set_option FROM pg_auth_members a "
        "JOIN pg_roles r ON r.oid=a.roleid JOIN pg_roles m ON m.oid=a.member "
        "JOIN pg_roles g ON g.oid=a.grantor WHERE r.rolname !~ '^pg_' OR m.rolname !~ '^pg_'",
        "role,member,grantor",
    )
    require(len(roles) <= 64 and len(members) <= 256, "role count limit")
    return dict(roles=roles, memberships=members)


def sequence_state(query):
    names = rows(
        query,
        "SELECT c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind='S'",
        "name",
    )
    require(len(names) <= MAX_TABLES)
    result = {}
    for item in names:
        name = item["name"]
        require(IDENTIFIER.fullmatch(name) is not None)
        result[name] = query(
            f"SELECT jsonb_build_object('last_value',last_value,'is_called',is_called) FROM public.\"{name}\""
        )
    return result


def database_state(query):
    require(
        query("SELECT to_jsonb(pg_database_size(current_database()))") <= MAX_DB,
        "source database size limit",
    )
    tables = rows(
        query,
        "SELECT c.relname AS name, pg_get_userbyid(c.relowner) AS owner, "
        "c.relrowsecurity AS rls, c.relforcerowsecurity AS force_rls, c.relacl AS acl, "
        "has_table_privilege('scheduler_app', c.oid, 'SELECT') AS app_select "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN ('r','p')",
        "name",
    )
    require(0 < len(tables) <= MAX_TABLES)
    counts, total = {}, 0
    for table in tables:
        name = table["name"]
        require(IDENTIFIER.fullmatch(name) is not None)
        count = query(
            f'SELECT to_jsonb(count(*)) FROM (SELECT 1 FROM public."{name}" LIMIT {MAX_ROWS + 1}) x'
        )
        require(type(count) is int and 0 <= count <= MAX_ROWS, "table row limit")
        counts[name] = count
        total += count
        require(total <= MAX_TOTAL_ROWS, "database row limit")
    # Every count/size is bounded before any content serialization or aggregation.
    fingerprints = {}
    for name, count in counts.items():
        value = query(
            "SELECT to_jsonb(encode(sha256(convert_to(COALESCE(string_agg(h,'' ORDER BY h),''),'UTF8')),'hex')) "
            f"FROM (SELECT encode(sha256(convert_to(to_jsonb(t)::text,'UTF8')),'hex') h FROM public.\"{name}\" t) x"
        )
        fingerprints[name] = dict(count=count, sha256=value)
    catalog = {}
    selections = {
        "policies": ("SELECT * FROM pg_policies WHERE schemaname='public'", "tablename,policyname"),
        "functions": (
            "SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS arguments, "
            "pg_get_userbyid(p.proowner) AS owner, p.proacl, pg_get_functiondef(p.oid) AS definition "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prokind='f'",
            "proname,arguments",
        ),
        "constraints": (
            "SELECT c.conname, c.conrelid::regclass::text AS relation, pg_get_constraintdef(c.oid) AS definition "
            "FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public'",
            "relation,conname",
        ),
        "indexes": (
            "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public'",
            "tablename,indexname",
        ),
        "triggers": (
            "SELECT t.tgname, c.relname, pg_get_triggerdef(t.oid) AS definition FROM pg_trigger t "
            "JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND NOT t.tgisinternal",
            "relname,tgname",
        ),
        "schemas": (
            "SELECT nspname,pg_get_userbyid(nspowner) AS owner,nspacl FROM pg_namespace WHERE nspname='public'",
            "nspname",
        ),
        "extensions": (
            "SELECT extname,extversion,CASE WHEN extname='plpgsql' THEN 'initdb' ELSE pg_get_userbyid(extowner) END AS owner FROM pg_extension",
            "extname",
        ),
        "database": (
            "SELECT datname,pg_get_userbyid(datdba) AS owner,encoding,datcollate,datctype,datacl "
            "FROM pg_database WHERE datname=current_database()",
            "datname",
        ),
    }
    for name, (selection, order) in selections.items():
        catalog[name] = digest(canonical(rows(query, selection, order)))
    head = query(
        "SELECT COALESCE(jsonb_agg(version_num ORDER BY version_num),'[]'::jsonb) FROM public.alembic_version"
    )
    require(
        len(head) == 1 and re.fullmatch(r"[0-9]{4}", head[0]) is not None, "migration head invalid"
    )
    references = rows(
        query,
        "SELECT blob_key AS key,checksum AS sha256,replace(tenant_id::text,'-','') AS tenant, "
        "replace(id::text,'-','') AS source,revision FROM public.knowledge_sources "
        "WHERE blob_key IS NOT NULL AND deleted_at IS NULL",
        "key",
    )
    return dict(
        tables=tables,
        fingerprints=fingerprints,
        catalog=catalog,
        migration_head=head,
        sequences=sequence_state(query),
        blob_refs=references,
    )


def check_references(state, blobs, secret):
    records, capture = blobs
    for ref in state["blob_refs"]:
        key = ref["key"]
        require(key in capture, "referenced original missing")
        payload = verify_receipt(key, records[key], records[key + ".receipt"], secret)
        require(
            (payload["tenant_id"], payload["source_id"], payload["revision"], payload["sha256"])
            == (ref["tenant"], ref["source"], ref["revision"], ref["sha256"]),
            "SQL blob identity mismatch",
        )
        require(payload["unreferenced_at"] is None, "referenced blob is retired")


def rls_queries(state):
    tables = [item for item in state["tables"] if item["rls"] and item["app_select"]]
    require(tables and all(item["force_rls"] for item in tables), "RLS tables required")
    # Check no-tenant and absent synthetic tenant scopes under the actual restricted role.
    for scope in ("", "00000000-0000-0000-0000-000000000000"):
        for item in tables:
            name = item["name"]
            require(IDENTIFIER.fullmatch(name) is not None)
            yield (
                "BEGIN READ ONLY; SET LOCAL ROLE scheduler_app; "
                f"SET LOCAL app.tenant_id='{scope}'; SELECT to_jsonb(count(*)) FROM public.\"{name}\"; ROLLBACK;"
            )
