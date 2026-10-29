/**
 * Минимальный разбор Markdown из ответа Claude: заголовки, списки, цитаты, таблицы, абзацы,
 * жирный, курсив и код. Результат — данные, а не HTML: React экранирует весь текст.
 */

export type Inline =
  | { kind: 'text'; text: string }
  | { kind: 'strong'; text: string }
  | { kind: 'em'; text: string }
  | { kind: 'code'; text: string };

export type Block =
  | { kind: 'heading'; level: number; inline: Inline[] }
  | { kind: 'paragraph'; inline: Inline[] }
  | { kind: 'list'; ordered: boolean; items: Inline[][] }
  | { kind: 'quote'; inline: Inline[] }
  | { kind: 'code'; text: string }
  | { kind: 'table'; align: Align[]; header: Inline[][]; rows: Inline[][][] }
  | { kind: 'rule' };

export type Align = 'left' | 'center' | 'right' | null;

const INLINE = /(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*\s][^*]*\*|_[^_\s][^_]*_)/g;

export function parseInline(text: string): Inline[] {
  const result: Inline[] = [];
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    const token = match[0];
    const index = match.index ?? 0;
    if (index > last) {
      result.push({ kind: 'text', text: text.slice(last, index) });
    }
    if (token.startsWith('**') || token.startsWith('__')) {
      result.push({ kind: 'strong', text: token.slice(2, -2) });
    } else if (token.startsWith('`')) {
      result.push({ kind: 'code', text: token.slice(1, -1) });
    } else {
      result.push({ kind: 'em', text: token.slice(1, -1) });
    }
    last = index + token.length;
  }
  if (last < text.length) {
    result.push({ kind: 'text', text: text.slice(last) });
  }
  return result;
}

/** Ячейки строки таблицы: крайние | необязательны, \| — символ внутри ячейки. */
function tableCells(line: string): string[] {
  let text = line.trim();
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|') && !text.endsWith('\\|')) text = text.slice(0, -1);
  return text.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, '|'));
}

const TABLE_DELIMITER = /^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$/;

function delimiterAlign(cell: string): Align {
  const left = cell.startsWith(':');
  const right = cell.endsWith(':');
  if (left && right) return 'center';
  if (right) return 'right';
  return left ? 'left' : null;
}

const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;

export function parseMarkdown(source: string): Block[] {
  const blocks: Block[] = [];
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  let paragraph: string[] = [];

  const flush = () => {
    if (paragraph.length) {
      blocks.push({ kind: 'paragraph', inline: parseInline(paragraph.join(' ')) });
      paragraph = [];
    }
  };

  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    const trimmed = line.trim();
    if (!trimmed) {
      flush();
      continue;
    }
    if (trimmed.startsWith('```')) {
      flush();
      const code: string[] = [];
      index++;
      while (index < lines.length && !lines[index].trim().startsWith('```')) {
        code.push(lines[index]);
        index++;
      }
      blocks.push({ kind: 'code', text: code.join('\n') });
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
    if (heading) {
      flush();
      blocks.push({ kind: 'heading', level: heading[1].length, inline: parseInline(heading[2]) });
      continue;
    }
    if (/^([-*_])(\s*\1){2,}$/.test(trimmed)) {
      flush();
      blocks.push({ kind: 'rule' });
      continue;
    }
    if (trimmed.startsWith('>')) {
      flush();
      blocks.push({ kind: 'quote', inline: parseInline(trimmed.replace(/^>\s?/, '')) });
      continue;
    }
    // Таблица: строка с | и сразу под ней строка-разделитель с тем же числом колонок.
    const delimiter = lines[index + 1]?.trim() ?? '';
    if (trimmed.includes('|') && TABLE_DELIMITER.test(delimiter)) {
      const header = tableCells(trimmed);
      const align = tableCells(delimiter).map(delimiterAlign);
      if (align.length === header.length) {
        flush();
        const rows: Inline[][][] = [];
        index += 2;
        while (index < lines.length && lines[index].trim().includes('|')) {
          const cells = tableCells(lines[index]);
          rows.push(header.map((_, column) => parseInline(cells[column] ?? '')));
          index++;
        }
        index--;
        blocks.push({ kind: 'table', align, header: header.map(parseInline), rows });
        continue;
      }
    }
    const bullet = BULLET.exec(line);
    const numbered = NUMBERED.exec(line);
    if (bullet || numbered) {
      flush();
      const ordered = Boolean(numbered && !bullet);
      const items: Inline[][] = [];
      while (index < lines.length) {
        const item = (ordered ? NUMBERED : BULLET).exec(lines[index]);
        if (!item) {
          break;
        }
        items.push(parseInline(item[1]));
        index++;
      }
      index--;
      blocks.push({ kind: 'list', ordered, items });
      continue;
    }
    paragraph.push(trimmed);
  }
  flush();
  return blocks;
}

/** Заголовок документа: первый заголовок верхнего уровня, если он есть. */
export function documentTitle(blocks: Block[]): string | null {
  const first = blocks.find((block) => block.kind === 'heading');
  return first && first.kind === 'heading' ? first.inline.map((part) => part.text).join('') : null;
}
