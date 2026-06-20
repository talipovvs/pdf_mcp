#!/usr/bin/env python3
"""
PDF Reports MCP Server — pure Python, newline-delimited JSON (no SDK).
Handles: initialize, ping, tools/list, tools/call
Calls pdf_generator.py for actual PDF generation.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPORTS_DIR = os.environ.get("REPORTS_DIR", "/agent-data/reports")
REPORTS_URL = os.environ.get("REPORTS_URL", "http://localhost:8090")

PARAM_SCHEMA = {
    "type": "object",
    "properties": {
        "object_name":    {"type": "string", "description": "Название объекта или проекта"},
        "object_address": {"type": "string", "description": "Адрес или локация объекта"},
        "content":        {"type": "string", "description": "Содержимое отчёта в формате Markdown: таблицы, заголовки ## и ###, маркированные списки. Минимум 200 символов."},
        "company_name":   {"type": "string", "description": "Название компании для шапки отчёта"},
        "object_type":    {"type": "string", "description": "Тип объекта: 'жилой' или 'коммерческий'. Используется для дисклеймера в отчёте.", "enum": ["жилой", "коммерческий"]},
    },
    "required": ["object_name", "object_address", "content", "company_name"],
}

TOOLS = [
    {
        "name": "generate_location_report",
        "description": (
            "Генерирует PDF-отчёт по анализу локации объекта. "
            "Структура content (строго по порядку): "
            "## Транспортная доступность — расстояния, магистрали, оценка/10; "
            "## Социальная инфраструктура — школы, магазины, медицина, оценка/10; "
            "## Конкуренты — таблица застройщиков и готовых объектов, конкурентность; "
            "## Характеристики района — газификация, водопровод, рельеф, экология, оценка/10; "
            "## Инвестиционные факторы — планы развития, динамика района, оценка/10; "
            "## Итоговая оценка — взвешенный балл из всех блоков; "
            "## Преимущества — топ-3 пункта; "
            "## Риски — топ-3 пункта; "
            "## Вывод — рекомендация и обоснование 2-3 предложения. "
            "Все цифры только из location.md — не придумывать."
        ),
        "inputSchema": PARAM_SCHEMA,
    },
    {
        "name": "generate_market_report",
        "description": (
            "Генерирует PDF-отчёт по исследованию рынка. "
            "Структура content (строго по порядку): "
            "Для коммерческого: "
            "## Ставки аренды — таблица мин/медиана/макс по классам A/B/C, тип ставки, дата; "
            "## Вакантность рынка — %, тренд (дефицит/баланс/профицит), новое строительство; "
            "## Стоимость строительства — таблица cold shell / warm shell / под ключ (руб./м²); "
            "## Активные арендаторы — отрасли и примеры компаний; "
            "## Динамика ставок — изменение за 12 месяцев; "
            "## Вывод — 1-2 предложения о состоянии рынка. "
            "Для жилого: "
            "## Строительство под ключ — мин/медиана/макс руб./м² по технологии; "
            "## Готовые дома — мин/медиана/макс руб./м², количество объявлений; "
            "## Участки ИЖС — мин/медиана/макс руб./сот; "
            "## Конкуренты — таблица компаний с технологией и ценой; "
            "## Динамика цен — изменение за 12 месяцев; "
            "## Вывод — 1-2 предложения. "
            "Все цифры только из market.md. ЗАПРЕЩЕНО рассчитывать NOI, cap rate, маржу, окупаемость."
        ),
        "inputSchema": PARAM_SCHEMA,
    },
    {
        "name": "get_current_date",
        "description": (
            "Возвращает сегодняшнюю дату из системных часов сервера. "
            "Используй этот инструмент в начале любого скилла который собирает данные с датой "
            "(market-research, location-analysis, macro-data, fin-model-prep, generate-pdf), "
            "чтобы получить достоверную дату и передать её субагенту или поставить в отчёт. "
            "ЗАПРЕЩЕНО придумывать дату вручную — модель не знает текущее время. "
            "Возвращает дату в формате ISO YYYY-MM-DD."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "generate_summary_report",
        "description": (
            "Генерирует итоговый PDF-отчёт для руководства. "
            "content — это составленный агентом markdown, НЕ копия файлов. "
            "ЗАПРЕЩЕНО: вставлять YAML-шапку (project_slug, prepared_at, sources); "
            "ЗАПРЕЩЕНО: QC-чеклисты ([x]/[ ]), пути к файлам (project.md, macro.md); "
            "ЗАПРЕЩЕНО: копировать finmodel.md целиком; "
            "ЗАПРЕЩЕНО: рассчитывать NOI, cap rate, IRR, окупаемость, маржу; "
            "ЗАПРЕЩЕНО: строить сценарии (пессимист/база/оптимист), задавать заполняемость, "
            "рекомендовать оптимальную ставку аренды — это задача финансового агента. "
            "Аналитик передаёт ТОЛЬКО рыночные факты. "
            "Структура content (строго по порядку, все разделы обязательны): "
            "## Ключевые параметры проекта — тип, класс, GBA/GLA, бюджет клиента, бюджет строительства (из project.md + market.md). "
            "Если бюджет клиента < стоимости строительства — добавь строку: ⚠ Разрыв финансирования: X млн руб.; "
            "## Состояние рынка — вакантность %, тренд, динамика ставок за 12 мес (из market.md); "
            "## Ставки аренды — таблица мин/медиана/макс по классам A/B/C (из market.md, как рыночный факт); "
            "## Потенциальные арендаторы — таблица отраслей и примеров компаний с активностью (из market.md); "
            "## Конкуренты — таблица объект/класс/площадь/ставка/вакантность (из location.md); "
            "## Стоимость строительства — таблица cold shell / warm shell / под ключ руб./м² (из market.md); "
            "## Финансирование — таблица банков со ставками и условиями (из finmodel.md раздел 8), выдели лучшую; "
            "## Макроэкономика — КС ЦБ, прогноз на 3 года, инфляция, НДС, налог на прибыль (из finmodel.md раздел 7); "
            "## Локация — оценка/10, таблица блоков с весами и оценками, топ-3 преимущества, топ-3 риска, инвест. факторы (из location.md); "
            "## Сроки реализации — этапы с длительностью в мес: проектирование, строительство, выход на заполняемость (из finmodel.md раздел 6); "
            "## Вывод аналитики — 3-5 пунктов: подходит ли локация, здоров ли рынок, есть ли спрос, ключевые риски. "
            "ЗАПРЕЩЕНО включать сюда выбор сценария, оптимальную ставку, маржу — только аналитические факты."
        ),
        "inputSchema": PARAM_SCHEMA,
    },
]

TOOL_NAMES = {t["name"] for t in TOOLS}


def send(obj: dict):
    line = json.dumps(obj, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def respond(id_, result):
    send({"jsonrpc": "2.0", "id": id_, "result": result})


def respond_error(id_, code, message):
    send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def validate_args(args: dict) -> str | None:
    """Validate tool arguments. Returns error message or None if valid."""
    content = args.get("content", "")
    if not content or len(content.strip()) < 200:
        return (
            "Параметр 'content' слишком короткий или пустой. "
            "Минимум 200 символов — передай полные данные из файлов проекта."
        )
    return None


def call_generator(tool: str, args: dict) -> str:
    error = validate_args(args)
    if error:
        raise ValueError(error)

    payload = json.dumps({"tool": tool, **args})
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "pdf_generator.py")],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "REPORTS_DIR": REPORTS_DIR, "REPORTS_URL": REPORTS_URL},
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "PDF generation failed")
    return result.stdout.strip()


def handle(msg: dict):
    method = msg.get("method", "")
    id_ = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        respond(id_, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "pdf-reports", "version": "1.0.0"},
        })

    elif method in ("initialized", "notifications/initialized"):
        pass 

    elif method == "ping":
        respond(id_, {})

    elif method == "tools/list":
        respond(id_, {"tools": TOOLS})

    elif method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        if name not in TOOL_NAMES:
            respond_error(id_, -32601, f"Unknown tool: {name}")
            return

        if name == "get_current_date":
            today = datetime.now().strftime("%Y-%m-%d")
            print(f"[pdf-mcp] tool=get_current_date -> {today}", file=sys.stderr, flush=True)
            respond(id_, {"content": [{"type": "text", "text": today}], "isError": False})
            return

        content_len = len((args.get("content") or "").strip())
        print(f"[pdf-mcp] tool={name} object={args.get('object_name','?')} content_len={content_len}", file=sys.stderr, flush=True)
        try:
            text = call_generator(name, args)
            print(f"[pdf-mcp] OK: {text[:80]}", file=sys.stderr, flush=True)
            respond(id_, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:
            print(f"[pdf-mcp] ERROR: {e}", file=sys.stderr, flush=True)
            respond(id_, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})

    elif id_ is not None:
        respond_error(id_, -32601, f"Method not found: {method}")


def main():
    print("[pdf-mcp] started", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"[pdf-mcp] parse error: {e}", file=sys.stderr, flush=True)
            continue
        handle(msg)


if __name__ == "__main__":
    main()
