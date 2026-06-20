#!/usr/bin/env python3
"""
Генератор PDF. Запускается MCP-сервером как отдельный процесс.
Параметры приходят в stdin как JSON, готовый файл сохраняется в /agent-data/reports,
а в stdout печатается ссылка для скачивания.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "/agent-data/reports"))
REPORTS_URL = os.environ.get("REPORTS_URL", "http://localhost:8090")

_SCRIPT_DIR = Path(__file__).parent
_FONTS_DIR = _SCRIPT_DIR / "fonts"

def _find_font(bundled: str, fallbacks: list) -> str:
    p = _FONTS_DIR / bundled
    if p.exists():
        return str(p)
    for f in fallbacks:
        if Path(f).exists():
            return f
    return str(p)

FONT_R  = _find_font("Arial.ttf",          ["/System/Library/Fonts/Supplemental/Arial.ttf"])
FONT_B  = _find_font("ArialBold.ttf",      ["/System/Library/Fonts/Supplemental/Arial Bold.ttf"])
FONT_I  = _find_font("ArialItalic.ttf",    ["/System/Library/Fonts/Supplemental/Arial Italic.ttf"])
FONT_BI = _find_font("ArialBoldItalic.ttf",["/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf"])


BLUE       = (26, 86, 160)
BLUE_LIGHT = (240, 244, 255)
DARK       = (26, 26, 26)
GRAY       = (136, 136, 136)
WHITE      = (255, 255, 255)
ROW_ALT    = (245, 248, 255)
BORDER     = (224, 224, 224)

_TR = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'zh','з':'z',
    'и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
    'с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts','ч':'ch','ш':'sh','щ':'sch',
    'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
}

def slugify(text: str) -> str:
    result = ''.join(_TR.get(c, c) for c in text.lower())
    return re.sub(r'[^a-z0-9]+', '-', result).strip('-')[:40]


class ReportPDF(FPDF):
    def __init__(self, company_name: str):
        super().__init__()
        self._company_name = company_name
        self._date_str = datetime.now().strftime("%d.%m.%Y")

    def footer(self):
        self.set_y(-15)
        self.set_draw_color(*BORDER)
        self.line(15, self.get_y(), 195, self.get_y())
        self.ln(2)
        self.set_font('Arial', 'I', 8)
        self.set_text_color(*GRAY)
        self.cell(
            0, 5,
            f'Отчёт сформирован автоматически · {self._company_name} · {self._date_str}'
            f'    Стр. {self.page_no()}/{{nb}}',
            align='C',
        )


def _strip_yaml_frontmatter(md: str) -> str:
    """Срезает YAML-frontmatter, если он есть в начале документа."""
    stripped = md.strip()
    if stripped.startswith('---'):
        # ищем закрывающий ---
        end = stripped.find('\n---', 3)
        if end != -1:
            return stripped[end + 4:].lstrip('\n')
    return md


def _strip_md(text: str) -> str:
    """Убирает звёздочки жирного и курсива."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    return text


def _fmt_num(s: str) -> str:
    """Расставляет пробелы между разрядами в больших числах."""
    clean = re.sub(r'\s', '', s)
    if re.fullmatch(r'\d+', clean):
        n = int(clean)
        if n >= 1000:
            return f"{n:,}".replace(',', ' ')   # разделитель — обычный пробел
    return s


def _is_percent_or_rate(text: str) -> bool:
    """Возвращает True, если в строке проценты или дробное число типа 13,8%."""
    return bool(re.match(r'^\d+[\.,]\d+\s*%?$', text.strip()))


def _fmt_cell(text: str) -> str:
    """Форматирует значение ячейки таблицы: расставляет пробелы в числах."""
    stripped = text.strip()
    # проценты и дробные ставки не трогаем — иначе сломаем диапазон вроде 
    if _is_percent_or_rate(stripped):
        return stripped
    # ячейка начинается с целого числа (потом может быть единица — ₽, м² и т.п.)
    m = re.match(r'^(\d[\d\s]*)(\s*[₽%]?\s*.*)$', stripped)
    if m:
        num_part = _fmt_num(m.group(1))
        rest = m.group(2)
        # между числом и единицей должен быть пробел
        # но не перед запятой/точкой/тире — иначе диапазон "13,0–15,0%" сломается
        if rest and not rest[0].isspace() and rest[0] not in (',', '.', '–', '-'):
            rest = ' ' + rest
        return num_part + rest
    # число где-то в середине — оставляем как есть
    return text


def _preprocess_md(md: str) -> str:
    """Чистит типовые косяки в Markdown от модели до того, как рендерить."""
    # символы, которые не умеет рисовать Arial
    # неразрывный дефис и hyphen → обычный минус
    # (модель шлёт U+2011 везде, Arial его молча выбрасывает — получается «618мес»)
    md = md.replace('‑', '-').replace('‐', '-')
    # знак рубля Arial тоже не умеет — меняем на «руб.»
    md = re.sub(r'\s*₽\s*', ' руб. ', md)   # заодно нормализуем пробелы вокруг
    md = re.sub(r'  +', ' ', md)                  # двойные пробелы → одиночные
    md = re.sub(r'\(\s*руб\.\s*/', '(руб./', md)  # «(руб. /м²)» → «(руб./м²)»

    # заголовки, прилипшие к концу предыдущего абзаца
    # «- Бюджет: 48 млн руб. ## Рынок» → две строки
    md = re.sub(r'(.+?)\s*(#{1,3} )', r'\1\n\2', md)

    # убираем одинокие «#» — артефакт от модели или от предыдущего split-а
    md = re.sub(r'^\s*#\s*$', '', md, flags=re.MULTILINE)

    lines = []
    for line in md.split('\n'):
        # диапазон цифр + единица, всё слиплось
        line = re.sub(
            r'(\d{2,4})(\d{2,4})(км|м²|м(?!\w)|лет|мес(?!\w)|мин(?!\w))',
            lambda m: f"{m.group(1)}–{m.group(2)} {m.group(3)}",
            line,
        )
        # диапазон из 3 цифр + единица времени
        # 618 месяцев это 51 год — явно слепленный диапазон, режем по первой цифре
        # (?<!\d) — чтобы не зацепить число побольше
        line = re.sub(
            r'(?<!\d)(\d)(\d{2})(мес(?!\w)|лет(?!\w)|мин(?!\w))',
            lambda m: f"{m.group(1)}–{m.group(2)} {m.group(3)}",
            line,
        )
        # пробел перед единицей: «25км» → «25 км»
        line = re.sub(
            r'(\d)(км|м²|м(?!\w)|лет|мес(?!\w)|мин(?!\w)|мес\.)',
            r'\1 \2',
            line,
        )
        # латиница слиплась с кириллицей
        line = re.sub(r'([A-Za-z])([а-яёА-ЯЁ])', r'\1 \2', line)
        # конец предложения склеился с началом следующего:
        # «вакантностиБазовый» → «вакантности Базовый»
        line = re.sub(r'([а-яё])([А-ЯЁ])', r'\1 \2', line)
        # нет пробела после двоеточия
        line = re.sub(r':([А-Яа-яA-Za-z])', r': \1', line)
        # второе число в диапазоне без разделителя разрядов:
        def _format_range_number(m):
            first = m.group(1)
            sep = m.group(2)
            second = m.group(3)
            # форматируем только второе число, первое уже в порядке
            try:
                n = int(second)
                if n >= 1000:
                    second = f"{n:,}".replace(',', ' ')
            except ValueError:
                pass
            return f"{first}{sep}{second}"
        line = re.sub(
            r'(\d[\d\s]*\d)\s*([-–—])\s*(\d{4,})',
            _format_range_number,
            line,
        )
        # слипшиеся слеш-команды
        line = re.sub(r'weekly\s*update', '/weekly-update', line, flags=re.IGNORECASE)
        line = re.sub(r'AI\s*аналитик', 'AI-аналитик', line, flags=re.IGNORECASE)
        line = re.sub(r'Топ\s*конкурент', 'Топ-конкурент', line)
        # пробел между цифрой и «руб.»
        line = re.sub(r'(\d)(руб\.)', r'\1 \2', line)
        lines.append(line)
    return '\n'.join(lines)

# Markdown в PDF

def render_markdown(pdf: ReportPDF, md: str):
    lines = md.split('\n')
    i = 0
    table_rows: list = []
    in_table = False

    while i < len(lines):
        line = lines[i]

        # строка таблицы
        if '|' in line and line.strip().startswith('|'):
            if not in_table:
                in_table = True
                table_rows = []
            # пропускаем строки-разделители вида |---|---|
            if re.match(r'^\s*\|[\s\-|:]+\|\s*$', line):
                i += 1
                continue
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            table_rows.append(cells)
            i += 1
            continue
        else:
            if in_table and table_rows:
                render_table(pdf, table_rows)
                table_rows = []
            in_table = False

        # пропускаем чек-листы внутренней QC-проверки
        if re.match(r'^[-*]\s*\[[ x]\]', line):
            i += 1
            continue

        # цитата (внутренняя пометка) — серым курсивом
        if line.startswith('> '):
            text = _strip_md(line[2:].strip())
            pdf.set_font('Arial', 'I', 9)
            pdf.set_text_color(*GRAY)
            pdf.set_x(18)
            pdf.multi_cell(0, 5, text)
            pdf.set_text_color(*DARK)
            i += 1
            continue

        # заголовки
        if line.startswith('## '):
            text = line[3:].strip()
            pdf.ln(4)
            pdf.set_fill_color(*BLUE)
            pdf.set_text_color(*WHITE)
            pdf.set_font('Arial', 'B', 12)
            pdf.set_x(15)
            pdf.cell(4, 8, '', fill=True)
            pdf.set_fill_color(*BLUE_LIGHT)
            pdf.set_text_color(*DARK)
            pdf.cell(165, 8, f'  {text}', fill=True)
            pdf.ln(10)

        elif line.startswith('### '):
            pdf.ln(2)
            pdf.set_text_color(*BLUE)
            pdf.set_font('Arial', 'B', 11)
            pdf.set_x(15)
            pdf.multi_cell(0, 7, line[4:].strip())
            pdf.ln(2)

        elif line.startswith('# '):
            pdf.ln(4)
            pdf.set_text_color(*BLUE)
            pdf.set_font('Arial', 'B', 14)
            pdf.set_x(15)
            pdf.multi_cell(0, 9, line[2:].strip())
            pdf.ln(4)

        # жирный ключ: значение
        elif line.startswith('**') and re.match(r'^\*\*(.+?)\*\*\s*:?\s*(.*)$', line):
            m = re.match(r'^\*\*(.+?)\*\*\s*:?\s*(.*)$', line)
            key = m.group(1).strip().rstrip(':')  # убираем двоеточие, чтобы не было «::»
            val = m.group(2).strip()
            key_text = key + ':'

            pdf.set_font('Arial', 'B', 10)
            pdf.set_text_color(*DARK)
            pdf.set_x(15)

            # смотрим, влезает ли ключ в одну строку рядом со значением.
            # если ключ длиннее 55 мм — он перекроет значение, поэтому ставим вертикально.
            key_width = pdf.get_string_width(key_text)

            if not val or key_width > 53:
                # длинный ключ или вообще без значения — ключ на всю строку, значение под ним
                pdf.multi_cell(0, 6, key_text)
                if val:
                    pdf.set_font('Arial', '', 10)
                    pdf.set_x(15)
                    pdf.multi_cell(0, 6, val)
            else:
                # короткий ключ — рисуем в одну строку с значением
                pdf.cell(55, 6, key_text)
                pdf.set_font('Arial', '', 10)
                x_after_key = pdf.get_x()
                # переносимые строки значения должны выравниваться под значением,
                # а не уходить под ключ
                pdf.set_left_margin(x_after_key)
                pdf.multi_cell(0, 6, val)
                pdf.set_left_margin(15)
            pdf.ln(1)

        # маркированный список
        elif line.startswith('- ') or line.startswith('* '):
            text = _strip_md(line[2:].strip())
            pdf.set_font('Arial', '', 10)
            pdf.set_text_color(*DARK)
            pdf.set_left_margin(25)
            pdf.set_x(18)
            pdf.cell(5, 6, '•')
            pdf.set_x(25)
            pdf.multi_cell(0, 6, text)
            pdf.set_left_margin(15)

        # нумерованный список
        elif re.match(r'^\d+\.\s', line):
            m = re.match(r'^(\d+)\.\s(.+)$', line)
            text = _strip_md(m.group(2).strip())
            pdf.set_font('Arial', '', 10)
            pdf.set_text_color(*DARK)
            pdf.set_left_margin(26)
            pdf.set_x(18)
            pdf.cell(6, 6, f'{m.group(1)}.')
            pdf.set_x(26)
            pdf.multi_cell(0, 6, text)
            pdf.set_left_margin(15)

        # разделитель
        elif line.strip() in ('---', '***', '___'):
            pdf.ln(2)
            pdf.set_draw_color(*BORDER)
            pdf.line(15, pdf.get_y(), 195, pdf.get_y())
            pdf.ln(4)

        # обычный абзац
        elif line.strip():
            text = _strip_md(line.strip())
            pdf.set_font('Arial', '', 10)
            pdf.set_text_color(*DARK)
            pdf.set_x(15)
            pdf.multi_cell(0, 6, text)

        else:
            if i > 0 and lines[i - 1].strip():
                pdf.ln(3)

        i += 1

    if in_table and table_rows:
        render_table(pdf, table_rows)


def _col_widths(col_count: int) -> list:
    """Ширины колонок в мм. Сумма всегда 180, веса зависят от количества колонок."""
    if col_count == 6:
        return [44, 22, 22, 22, 40, 30]
    if col_count == 5:
        return [54, 28, 28, 28, 42]
    if col_count == 4:
        return [50, 46, 46, 38]
    if col_count == 3:
        return [65, 60, 55]
    if col_count == 2:
        return [90, 90]
    w = 180 / col_count
    return [w] * col_count


def _estimate_cell_lines(pdf: ReportPDF, text: str, usable_w: float, font_size: int) -> int:
    """Прикидывает, сколько строк займёт текст в ячейке заданной ширины."""
    pdf.set_font('Arial', '', font_size)
    if usable_w <= 0 or not text:
        return 1
    text_w = pdf.get_string_width(text)
    return max(1, int(text_w / usable_w) + 1)


def render_table(pdf: ReportPDF, rows: list):
    if not rows:
        return
    pdf.ln(2)
    col_count = max(len(r) for r in rows)
    widths = _col_widths(col_count)
    font_size = 8 if col_count >= 5 else 9
    line_h = 5 if col_count >= 5 else 6
    x_start = 15

    # колонки с деньгами — те, у кого в заголовке есть «руб»
    header_cells = rows[0] if rows else []
    currency_cols = {j for j, h in enumerate(header_cells) if 'руб' in h.lower()}

    # шапка таблицы — одной строкой, на синем фоне
    pdf.set_fill_color(*BLUE)
    pdf.set_text_color(*WHITE)
    pdf.set_font('Arial', 'B', font_size)
    hdr_h = line_h + 2
    pdf.set_x(x_start)
    for j, cell in enumerate(rows[0]):
        w = widths[j] if j < len(widths) else widths[-1]
        # в шапке режем длинный текст по букве, чтобы влез в одну строку
        pdf.set_font('Arial', 'B', font_size)
        while pdf.get_string_width(cell) > w - 2 and len(cell) > 1:
            cell = cell[:-1]
        pdf.cell(w, hdr_h, cell, fill=True, border=0)
    pdf.ln(hdr_h)

    # строки данных — с переносами, если текст не влезает
    for ri, row in enumerate(rows[1:]):
        fill_color = ROW_ALT if ri % 2 == 0 else WHITE

        # собираем тексты ячеек
        cells = []
        for j in range(col_count):
            raw = row[j].strip() if j < len(row) else ''
            cell = _strip_md(raw)
            cell = _fmt_cell(cell)
            if j in currency_cols and re.fullmatch(r'[\d\s]+', cell):
                cell = cell.strip() + ' руб.'
            cells.append(cell)

        # высота строки = максимум из высот её ячеек
        row_h = line_h
        for j, cell_text in enumerate(cells):
            w = widths[j] if j < len(widths) else widths[-1]
            lines = _estimate_cell_lines(pdf, cell_text, w - 3, font_size)
            row_h = max(row_h, lines * line_h)
        row_h += 2  # отступ

        # если строка не влезает на страницу — переносим до отрисовки
        if pdf.get_y() + row_h > pdf.h - pdf.b_margin:
            pdf.add_page()

        y0 = pdf.get_y()

        # фон под всей строкой
        total_w = sum(widths[:col_count])
        pdf.set_fill_color(*fill_color)
        pdf.rect(x_start, y0, total_w, row_h, 'F')

        # пока рисуем ячейки — отключаем авто-перенос страницы,
        # перенос мы уже обработали выше
        pdf.set_auto_page_break(auto=False)

        # рисуем каждую ячейку с переносом текста
        pdf.set_text_color(*DARK)
        pdf.set_font('Arial', '', font_size)
        x_cur = x_start
        for j, cell_text in enumerate(cells):
            w = widths[j] if j < len(widths) else widths[-1]
            pdf.set_xy(x_cur, y0 + 1)
            pdf.set_left_margin(x_cur)
            pdf.set_right_margin(210 - (x_cur + w))
            pdf.multi_cell(w, line_h, cell_text, align='L')
            x_cur += w

        # возвращаем авто-перенос и поля, спускаемся в конец строки
        pdf.set_auto_page_break(auto=True, margin=20)
        pdf.set_left_margin(15)
        pdf.set_right_margin(15)
        pdf.set_y(y0 + row_h)

        # линия-разделитель между строками
        pdf.set_draw_color(*BORDER)
        pdf.line(x_start, pdf.get_y(), 195, pdf.get_y())

    pdf.ln(4)

#Сборка готового PDF
def generate_pdf(report_title, report_type, object_name, object_address,
                 content_md, filename, company_name):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    pdf = ReportPDF(company_name)
    pdf.alias_nb_pages()
    pdf.set_margins(15, 20, 15)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_font('Arial', '',   FONT_R,  uni=True)
    pdf.add_font('Arial', 'B',  FONT_B,  uni=True)
    pdf.add_font('Arial', 'I',  FONT_I,  uni=True)
    pdf.add_font('Arial', 'BI', FONT_BI, uni=True)
    pdf.add_page()

    # синяя плашка сверху
    pdf.set_fill_color(*BLUE)
    pdf.rect(0, 0, 210, 12, 'F')

    # название компании на плашке (белым)
    pdf.set_y(4)
    pdf.set_font('Arial', 'I', 8)
    pdf.set_text_color(*WHITE)
    pdf.cell(0, 5, f'{company_name} — Аналитический отчёт', align='C')

    # заголовок отчёта — не шире 115 мм, иначе налезет на бейдж справа
    pdf.set_xy(15, 16)
    pdf.set_font('Arial', 'B', 20)
    pdf.set_text_color(*BLUE)
    pdf.cell(115, 10, report_title, align='L')

    # бейдж справа сверху (на той же высоте Y, что и заголовок)
    bx, by = 133, 15
    pdf.set_xy(bx, by)
    pdf.set_fill_color(*BLUE)
    pdf.set_text_color(*WHITE)
    pdf.set_font('Arial', 'B', 9)
    pdf.cell(57, 7, f'  {report_type}  ', fill=True, align='C')
    for offset, label in [(9, company_name), (16, f'Дата: {datetime.now().strftime("%d.%m.%Y")}')]:
        pdf.set_xy(bx, by + offset)
        pdf.set_text_color(85, 85, 85)
        pdf.set_font('Arial', '', 9)
        pdf.cell(57, 6, label, align='C')

    # подзаголовок жёстко на Y=38 — ниже бейджа
    pdf.set_font('Arial', '', 10)
    pdf.set_text_color(85, 85, 85)
    subtitle = f'{object_name}  |  {object_address}'
    pdf.set_xy(15, 38)
    # если не влезает — режем по букве, чтобы не залезть на колонку с бейджем
    max_sub_w = 115.0
    pdf.set_font('Arial', '', 10)
    while pdf.get_string_width(subtitle) > max_sub_w and len(subtitle) > 4:
        subtitle = subtitle[:-1]
    if subtitle != f'{object_name}  |  {object_address}':
        subtitle = subtitle.rstrip() + '…'
    pdf.cell(max_sub_w, 6, subtitle, align='L')

    # линия под шапкой
    pdf.set_y(44)
    pdf.set_draw_color(*BLUE)
    pdf.set_line_width(1)
    pdf.line(15, 44, 195, 44)
    pdf.set_line_width(0.3)

    # с этого места — содержимое отчёта
    pdf.set_y(52)
    content_md = _strip_yaml_frontmatter(content_md)
    preprocessed = _preprocess_md(content_md)
    # отладка: сохраняем обработанный markdown — если что-то странное в PDF,
    # можно посмотреть, что реально отдавали в рендер
    debug_path = REPORTS_DIR / "debug_last_content.txt"
    try:
        debug_path.write_text(
            f"=== RAW ===\n{content_md}\n\n=== PREPROCESSED ===\n{preprocessed}\n",
            encoding='utf-8'
        )
    except Exception:
        pass
    render_markdown(pdf, preprocessed)

    output_path = REPORTS_DIR / filename
    pdf.output(str(output_path))
    return str(output_path)

if __name__ == '__main__':
    data = json.loads(sys.stdin.read())

    tool = data.get('tool', 'generate_location_report')
    type_map = {
        'generate_location_report': ('Анализ локации',            'Локация',  'location'),
        'generate_market_report':   ('Исследование рынка',        'Рынок',    'market'),
        'generate_summary_report':  ('Итоговый отчёт по объекту', 'Итоговый', 'summary'),
    }
    report_title, report_type, suffix = type_map.get(tool, ('Отчёт', 'Отчёт', 'report'))

    filename = f"{slugify(data['object_name'])}-{suffix}-{datetime.now().strftime('%Y%m%d-%H%M')}.pdf"

    generate_pdf(
        report_title=report_title,
        report_type=report_type,
        object_name=data['object_name'],
        object_address=data['object_address'],
        content_md=data['content'],
        filename=filename,
        company_name=data['company_name'],
    )

    url = f"{REPORTS_URL}/{filename}"
    print(f"✅ PDF отчёт готов: [{filename}]({url})\n\nНажми на ссылку чтобы открыть → {url}")
