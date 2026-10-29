import json
import re
from pathlib import Path

import requests


FILTER_KEYS = {
    "dias_maximos", "salario_minimo_remoto", "salario_minimo_presencial",
    "distancia_maxima_km", "rejeitar_hibrido", "rejeitar_pj_mei",
    "rejeitar_temporario", "rejeitar_vagas_exclusivas_pcd",
    "rejeitar_comissao_sem_salario_base", "somente_home_office_nesta_automacao",
    "prioridades", "palavras_excluidas",
}


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _write(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _schema():
    return {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["add_workspace", "rename_workspace", "add_manual_source", "set_filter"]},
                        "id": {"type": "string"}, "label": {"type": "string"},
                        "workspace": {"type": "string"}, "name": {"type": "string"},
                        "url": {"type": "string"}, "key": {"type": "string"},
                        "value": {}, "countries": {"type": "array", "items": {"type": "string"}},
                        "remote_only": {"type": "boolean"},
                    },
                    "required": ["type"],
                },
            },
        },
        "required": ["message", "actions"],
    }


def plan(message: str, config_dir: Path, ai: dict):
    message = message.strip()[:4000]
    if not message:
        return {"message": "Descreva a mudança desejada.", "actions": []}
    current = {
        "workspaces": _read(config_dir / "workspaces.json", {"workspaces": []}),
        "filters": _read(config_dir / "filtros.json", {}),
    }
    if not ai.get("enabled"):
        return {"message": "Ative a IA local nas configurações para gerar um plano. Nenhuma alteração foi aplicada.", "actions": []}
    system = (
        "Você configura um sistema local de busca de vagas. Gere somente ações do schema. "
        "Nunca remova fontes, afrouxe regras de segurança, invente dados pessoais ou aplique alterações. "
        "Workspaces representam mercados/países. add_manual_source exige URL https. "
        "set_filter só pode usar chaves fornecidas. Explique o plano em português claro."
    )
    response = requests.post(
        ai.get("url", "http://ollama:11434") + "/api/generate",
        timeout=90,
        json={
            "model": ai.get("model", "qwen2.5:1.5b"), "stream": False,
            "format": _schema(), "system": system,
            "prompt": json.dumps({"request": message, "current": current, "allowed_filter_keys": sorted(FILTER_KEYS)}, ensure_ascii=False),
            "options": {"temperature": 0}, "keep_alive": "5m",
        },
    )
    response.raise_for_status()
    result = json.loads(response.json()["response"])
    result["actions"] = validate_actions(result.get("actions", []))
    return result


def validate_actions(actions):
    valid = []
    for raw in actions[:20]:
        action = dict(raw)
        kind = action.get("type")
        if kind == "add_workspace":
            workspace_id = re.sub(r"[^a-z0-9_-]", "-", str(action.get("id", "")).lower()).strip("-")[:32]
            label = str(action.get("label", "")).strip()[:60]
            if workspace_id and label:
                valid.append({"type": kind, "id": workspace_id, "label": label, "countries": [str(x)[:40] for x in action.get("countries", [])[:20]], "remote_only": bool(action.get("remote_only", True))})
        elif kind == "rename_workspace":
            if re.fullmatch(r"[a-z0-9_-]{1,32}", str(action.get("id", ""))) and str(action.get("label", "")).strip():
                valid.append({"type": kind, "id": action["id"], "label": str(action["label"])[:60]})
        elif kind == "add_manual_source":
            url = str(action.get("url", "")).strip()
            if re.fullmatch(r"https://[^\s]+", url) and re.fullmatch(r"[a-z0-9_-]{1,32}", str(action.get("workspace", ""))):
                valid.append({"type": kind, "workspace": action["workspace"], "name": str(action.get("name", "Nova fonte"))[:80], "url": url[:1000]})
        elif kind == "set_filter" and action.get("key") in FILTER_KEYS:
            valid.append({"type": kind, "key": action["key"], "value": action.get("value")})
    return valid


def apply(actions, config_dir: Path):
    actions = validate_actions(actions)
    workspace_path = config_dir / "workspaces.json"
    filter_path = config_dir / "filtros.json"
    workspaces = _read(workspace_path, {"workspaces": []})
    filters = _read(filter_path, {})
    changed = []
    for action in actions:
        kind = action["type"]
        if kind == "add_workspace":
            if not any(x.get("id") == action["id"] for x in workspaces["workspaces"]):
                workspaces["workspaces"].append({"id": action["id"], "label": action["label"], "countries": action["countries"], "remote_only": action["remote_only"], "manual_sources": []})
                changed.append(f"Workspace {action['label']} adicionado")
        elif kind == "rename_workspace":
            for workspace in workspaces["workspaces"]:
                if workspace.get("id") == action["id"]:
                    workspace["label"] = action["label"]
                    changed.append(f"Workspace {action['id']} renomeado")
        elif kind == "add_manual_source":
            for workspace in workspaces["workspaces"]:
                if workspace.get("id") == action["workspace"]:
                    workspace.setdefault("manual_sources", [])
                    if not any(x.get("url") == action["url"] for x in workspace["manual_sources"]):
                        workspace["manual_sources"].append({"nome": action["name"], "url": action["url"]})
                        changed.append(f"Fonte {action['name']} adicionada")
        elif kind == "set_filter":
            filters[action["key"]] = action["value"]
            changed.append(f"Filtro {action['key']} atualizado")
    if changed:
        _write(workspace_path, workspaces)
        _write(filter_path, filters)
    return changed
