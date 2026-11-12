import { markdown, personaInfo, sections, type Skill, type Version, type Format } from "./model.ts";

declare global {
  // The existing CoreTeams format templates are loaded once in main.tsx.
  var CoreTeamsExport: {
    create: (format: Format, model: ExportModel) => Promise<Blob>;
  } | undefined;
}

type ExportModel = {
  title: string;
  date: string;
  persona: string;
  version: number;
  sections: { title: string; lines: string[] }[];
  markdown: string;
};

export function downloadBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export async function downloadVersion(version: Version, format: Format) {
  if (!globalThis.CoreTeamsExport) throw new Error("Шаблоны файлов не загрузились");
  const model: ExportModel = {
    title: "Результат по задаче",
    date: version.date,
    persona: personaInfo[version.snapshot.persona].name,
    version: version.n,
    sections: sections(version),
    markdown: markdown(version),
  };
  const blob = await globalThis.CoreTeamsExport.create(format, model);
  downloadBlob(blob, `CoreTeams-результат-версия-${version.n}.${format}`);
}

function crc32(bytes: Uint8Array) {
  let value = 0xffffffff;
  for (const byte of bytes) {
    value ^= byte;
    for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ (value & 1 ? 0xedb88320 : 0);
  }
  return (value ^ 0xffffffff) >>> 0;
}

export function zipBlob(files: Record<string, string>) {
  const encoder = new TextEncoder();
  const localParts: BlobPart[] = [];
  const centralParts: BlobPart[] = [];
  let offset = 0;
  let centralSize = 0;
  for (const [path, content] of Object.entries(files)) {
    const name = encoder.encode(path);
    const bytes = encoder.encode(content);
    const checksum = crc32(bytes);
    const local = new Uint8Array(30 + name.length);
    const lh = new DataView(local.buffer);
    lh.setUint32(0, 0x04034b50, true);
    lh.setUint16(4, 20, true);
    lh.setUint16(6, 0x0800, true);
    lh.setUint32(14, checksum, true);
    lh.setUint32(18, bytes.length, true);
    lh.setUint32(22, bytes.length, true);
    lh.setUint16(26, name.length, true);
    local.set(name, 30);
    localParts.push(local, bytes);

    const central = new Uint8Array(46 + name.length);
    const ch = new DataView(central.buffer);
    ch.setUint32(0, 0x02014b50, true);
    ch.setUint16(4, 20, true);
    ch.setUint16(8, 0x0800, true);
    ch.setUint32(16, checksum, true);
    ch.setUint32(20, bytes.length, true);
    ch.setUint32(24, bytes.length, true);
    ch.setUint16(28, name.length, true);
    ch.setUint32(42, offset, true);
    central.set(name, 46);
    centralParts.push(central);
    offset += local.length + bytes.length;
    centralSize += central.length;
  }
  const end = new Uint8Array(22);
  const eh = new DataView(end.buffer);
  eh.setUint32(0, 0x06054b50, true);
  eh.setUint16(8, centralParts.length, true);
  eh.setUint16(10, centralParts.length, true);
  eh.setUint32(12, centralSize, true);
  eh.setUint32(16, offset, true);
  return new Blob([...localParts, ...centralParts, end], { type: "application/zip" });
}

export function downloadSkill(skill: Skill) {
  downloadBlob(zipBlob(skill.files), `${skill.slug}-версия-${skill.version}.zip`);
}
