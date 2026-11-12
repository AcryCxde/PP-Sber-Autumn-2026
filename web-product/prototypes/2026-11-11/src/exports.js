/* Локальные шаблоны файлов CoreTeams. Они оформляют введённые данные, но не анализируют их. */
(() => {
  'use strict';
  const theme = Object.freeze({ ink: '152B35', blue: '2459E8', green: '29644C', pale: 'EAF2ED', muted: '61757A', white: 'FFFFFF' });
  const xml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' })[c]);
  const encoder = new TextEncoder();
  function crc32(bytes) {
    let value = 0xffffffff;
    for (const byte of bytes) { value ^= byte; for (let i = 0; i < 8; i++) value = (value >>> 1) ^ (value & 1 ? 0xedb88320 : 0); }
    return (value ^ 0xffffffff) >>> 0;
  }
  function zip(files, mime) {
    const local = [], central = [];
    let offset = 0, centralSize = 0;
    for (const [path, content] of Object.entries(files)) {
      const name = encoder.encode(path), bytes = typeof content === 'string' ? encoder.encode(content) : content;
      const checksum = crc32(bytes), a = new Uint8Array(30 + name.length), av = new DataView(a.buffer);
      av.setUint32(0, 0x04034b50, true); av.setUint16(4, 20, true); av.setUint16(6, 0x0800, true);
      av.setUint32(14, checksum, true); av.setUint32(18, bytes.length, true); av.setUint32(22, bytes.length, true); av.setUint16(26, name.length, true); a.set(name, 30);
      const b = new Uint8Array(46 + name.length), bv = new DataView(b.buffer);
      bv.setUint32(0, 0x02014b50, true); bv.setUint16(4, 20, true); bv.setUint16(6, 20, true); bv.setUint16(8, 0x0800, true);
      bv.setUint32(16, checksum, true); bv.setUint32(20, bytes.length, true); bv.setUint32(24, bytes.length, true); bv.setUint16(28, name.length, true); bv.setUint32(42, offset, true); b.set(name, 46);
      local.push(a, bytes); central.push(b); offset += a.length + bytes.length; centralSize += b.length;
    }
    const end = new Uint8Array(22), ev = new DataView(end.buffer);
    ev.setUint32(0, 0x06054b50, true); ev.setUint16(8, central.length, true); ev.setUint16(10, central.length, true); ev.setUint32(12, centralSize, true); ev.setUint32(16, offset, true);
    return new Blob([...local, ...central, end], { type: mime });
  }
  const rootRels = officeType => `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="${officeType}"/></Relationships>`;
  const coreProps = model => `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>${xml(model.title)}</dc:title><dc:creator>CoreTeams</dc:creator><dc:description>Локальный макет без анализа и серверного сохранения</dc:description></cp:coreProperties>`;

  function docx(model) {
    const para = (text, style = 'Normal') => `<w:p><w:pPr><w:pStyle w:val="${style}"/></w:pPr><w:r><w:t xml:space="preserve">${xml(text)}</w:t></w:r></w:p>`;
    const body = [para('CORETEAMS · РЕЗУЛЬТАТ', 'Kicker'), para(model.title, 'Title'), para(`${model.date} · ${model.persona} · версия ${model.version}`, 'Subtitle'), para('Локальный макет. Анализ файлов и работа агентов не выполнялись.', 'Notice'), ...model.sections.flatMap(section => [para(section.title, 'Heading1'), ...section.lines.map(line => para(line))])].join('');
    const files = {
      '[Content_Types].xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>`,
      '_rels/.rels': rootRels('word/document.xml'),
      'docProps/core.xml': coreProps(model),
      'word/_rels/document.xml.rels': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>`,
      'word/document.xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>${body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1100" w:right="1150" w:bottom="1100" w:left="1150" w:header="500" w:footer="500" w:gutter="0"/></w:sectPr></w:body></w:document>`,
      'word/styles.xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos" w:cs="Arial"/><w:sz w:val="22"/><w:color w:val="${theme.ink}"/></w:rPr></w:rPrDefault></w:docDefaults><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:pPr><w:spacing w:after="160" w:line="320" w:lineRule="auto"/></w:pPr></w:style><w:style w:type="paragraph" w:styleId="Kicker"><w:name w:val="Kicker"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:color w:val="${theme.green}"/><w:sz w:val="18"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="180" w:after="260"/></w:pPr><w:rPr><w:b/><w:color w:val="${theme.ink}"/><w:sz w:val="42"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:rPr><w:color w:val="${theme.muted}"/><w:sz w:val="20"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Notice"><w:name w:val="Notice"/><w:basedOn w:val="Normal"/><w:rPr><w:color w:val="${theme.muted}"/><w:i/><w:sz w:val="18"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="320" w:after="130"/></w:pPr><w:rPr><w:b/><w:color w:val="${theme.blue}"/><w:sz w:val="28"/></w:rPr></w:style></w:styles>`
    };
    return zip(files, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document');
  }

  function xlsx(model) {
    const rows = [
      [model.title, ''], [`Версия ${model.version} · ${model.date}`, model.persona],
      ['Локальный макет', 'Анализ файлов и работа агентов не выполнялись.'],
      ...model.sections.flatMap(section => [[section.title, ''], ...section.lines.flatMap(line => (String(line).match(/[\s\S]{1,30000}/g) || ['']).map(part => ['', part]))])
    ];
    const cell = (col, row, value, style) => `<c r="${col}${row}" s="${style}" t="inlineStr"><is><t xml:space="preserve">${xml(value)}</t></is></c>`;
    const sheetRows = rows.map((values, i) => `<row r="${i + 1}" ht="${i === 0 ? 34 : 29}" customHeight="1">${cell('A', i + 1, values[0], i === 0 ? 1 : values[0] && !values[1] ? 2 : 0)}${cell('B', i + 1, values[1], 0)}</row>`).join('');
    const files = {
      '[Content_Types].xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>`,
      '_rels/.rels': rootRels('xl/workbook.xml'), 'docProps/core.xml': coreProps(model),
      'xl/workbook.xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Результат" sheetId="1" r:id="rId1"/></sheets></workbook>`,
      'xl/_rels/workbook.xml.rels': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>`,
      'xl/worksheets/sheet1.xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"/></sheetViews><sheetFormatPr defaultRowHeight="19"/><cols><col min="1" max="1" width="31" customWidth="1"/><col min="2" max="2" width="95" customWidth="1"/></cols><sheetData>${sheetRows}</sheetData><pageMargins left="0.4" right="0.4" top="0.6" bottom="0.6" header="0.2" footer="0.2"/></worksheet>`,
      'xl/styles.xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="3"><font><sz val="11"/><name val="Aptos"/><color rgb="FF${theme.ink}"/></font><font><b/><sz val="16"/><name val="Aptos"/><color rgb="FF${theme.white}"/></font><font><b/><sz val="11"/><name val="Aptos"/><color rgb="FF${theme.green}"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF${theme.blue}"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>`
    };
    return zip(files, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
  }

  const slideShape = (id, name, x, y, w, h, text, size, color, fill) => `<p:sp><p:nvSpPr><p:cNvPr id="${id}" name="${xml(name)}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="${x}" y="${y}"/><a:ext cx="${w}" cy="${h}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>${fill ? `<a:solidFill><a:srgbClr val="${fill}"/></a:solidFill>` : '<a:noFill/>'}<a:ln><a:noFill/></a:ln></p:spPr><p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0"/><a:lstStyle/><a:p><a:pPr algn="l"/><a:r><a:rPr lang="ru-RU" sz="${size * 100}" b="${size >= 28 ? 1 : 0}"><a:solidFill><a:srgbClr val="${color}"/></a:solidFill><a:latin typeface="Aptos"/></a:rPr><a:t>${xml(text)}</a:t></a:r><a:endParaRPr lang="ru-RU"/></a:p></p:txBody></p:sp>`;
  function pptx(model) {
    const slides = [{ title: model.title, lines: [`Версия ${model.version} · ${model.date}`, model.persona, 'Локальный макет без анализа и запуска агентов'] }];
    for (const section of model.sections) {
      const wrapped = section.lines.flatMap(line => String(line).match(/.{1,85}(?:\s|$)|.{1,85}/gu) || ['']);
      for (let offset = 0; offset < wrapped.length; offset += 11) {
        slides.push({ title: offset ? `${section.title} · продолжение` : section.title, lines: wrapped.slice(offset, offset + 11) });
      }
    }
    const files = {
      '[Content_Types].xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/><Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/><Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>${slides.map((_, i) => `<Override PartName="/ppt/slides/slide${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>`).join('')}<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>`,
      '_rels/.rels': rootRels('ppt/presentation.xml'), 'docProps/core.xml': coreProps(model),
      'ppt/presentation.xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst><p:sldIdLst>${slides.map((_, i) => `<p:sldId id="${256 + i}" r:id="rId${i + 2}"/>`).join('')}</p:sldIdLst><p:sldSz cx="12192000" cy="6858000" type="screen16x9"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>`,
      'ppt/_rels/presentation.xml.rels': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>${slides.map((_, i) => `<Relationship Id="rId${i + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide${i + 1}.xml"/>`).join('')}</Relationships>`,
      'ppt/slideMasters/slideMaster1.xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMap accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" bg1="lt1" bg2="lt2" folHlink="folHlink" hlink="hlink" tx1="dk1" tx2="dk2"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>`,
      'ppt/slideMasters/_rels/slideMaster1.xml.rels': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>`,
      'ppt/slideLayouts/slideLayout1.xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1"><p:cSld name="Пустой"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>`,
      'ppt/slideLayouts/_rels/slideLayout1.xml.rels': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>`,
      'ppt/theme/theme1.xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="CoreTeams"><a:themeElements><a:clrScheme name="CoreTeams"><a:dk1><a:srgbClr val="${theme.ink}"/></a:dk1><a:lt1><a:srgbClr val="${theme.white}"/></a:lt1><a:dk2><a:srgbClr val="${theme.ink}"/></a:dk2><a:lt2><a:srgbClr val="F7F7F3"/></a:lt2><a:accent1><a:srgbClr val="${theme.blue}"/></a:accent1><a:accent2><a:srgbClr val="${theme.green}"/></a:accent2><a:accent3><a:srgbClr val="${theme.pale}"/></a:accent3><a:accent4><a:srgbClr val="${theme.muted}"/></a:accent4><a:accent5><a:srgbClr val="D8E5DB"/></a:accent5><a:accent6><a:srgbClr val="A4C6B0"/></a:accent6><a:hlink><a:srgbClr val="${theme.blue}"/></a:hlink><a:folHlink><a:srgbClr val="${theme.green}"/></a:folHlink></a:clrScheme><a:fontScheme name="CoreTeams"><a:majorFont><a:latin typeface="Aptos Display"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Aptos"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme><a:fmtScheme name="CoreTeams"><a:fillStyleLst><a:solidFill><a:schemeClr val="accent1"/></a:solidFill><a:solidFill><a:schemeClr val="accent2"/></a:solidFill><a:solidFill><a:schemeClr val="accent3"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="accent1"/></a:solidFill></a:ln><a:ln w="25400"><a:solidFill><a:schemeClr val="accent2"/></a:solidFill></a:ln><a:ln w="38100"><a:solidFill><a:schemeClr val="accent3"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="lt1"/></a:solidFill><a:solidFill><a:schemeClr val="lt2"/></a:solidFill><a:solidFill><a:schemeClr val="accent3"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>`
    };
    slides.forEach((slide, i) => {
      const lines = slide.lines;
      const shapes = [
        slideShape(2, 'Верхняя полоса', 0, 0, 12192000, 170000, '', 12, theme.white, theme.blue),
        slideShape(3, 'Метка', 620000, 500000, 10500000, 400000, `CORETEAMS · ${String(i + 1).padStart(2, '0')}`, 13, theme.green),
        slideShape(4, 'Заголовок', 620000, 1130000, 10700000, 1150000, slide.title.slice(0, 125), i === 0 ? 34 : 30, theme.ink),
        ...lines.map((line, j) => slideShape(5 + j, `Строка ${j + 1}`, 670000, 2450000 + j * 340000, 10700000, 330000, line, i === 0 ? 19 : 17, theme.ink)),
        slideShape(100, 'Нижняя подпись', 620000, 6350000, 10800000, 260000, `Версия ${model.version} · локальный макет`, 11, theme.muted)
      ].join('');
      files[`ppt/slides/slide${i + 1}.xml`] = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>${shapes}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>`;
      files[`ppt/slides/_rels/slide${i + 1}.xml.rels`] = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>`;
    });
    return zip(files, 'application/vnd.openxmlformats-officedocument.presentationml.presentation');
  }

  async function pdf(model) {
    const pages = [];
    let canvas, ctx, y;
    function page() {
      canvas = document.createElement('canvas'); canvas.width = 1190; canvas.height = 1684;
      ctx = canvas.getContext('2d'); ctx.scale(2, 2); ctx.fillStyle = '#FFFFFF'; ctx.fillRect(0, 0, 595, 842);
      ctx.fillStyle = `#${theme.blue}`; ctx.fillRect(0, 0, 595, 7);
      ctx.font = 'bold 11px Arial'; ctx.fillStyle = `#${theme.green}`; ctx.fillText('CORETEAMS · РЕЗУЛЬТАТ', 45, 52);
      ctx.font = '10px Arial'; ctx.fillStyle = `#${theme.muted}`; ctx.fillText(`Версия ${model.version} · локальный макет`, 45, 813);
      y = 78; pages.push(canvas);
    }
    function lines(text, font, color, lineHeight, maxWidth = 505) {
      ctx.font = font; ctx.fillStyle = color;
      for (const paragraph of String(text).split('\n')) {
        const words = paragraph.split(/\s+/).filter(Boolean); let line = '';
        for (const word of words) {
          const next = line ? `${line} ${word}` : word;
          if (ctx.measureText(next).width > maxWidth && line) { if (y > 775) page(); ctx.font = font; ctx.fillStyle = color; ctx.fillText(line, 45, y); y += lineHeight; line = word; } else line = next;
        }
        if (line) { if (y > 775) page(); ctx.font = font; ctx.fillStyle = color; ctx.fillText(line, 45, y); y += lineHeight; }
        if (!words.length) y += lineHeight / 2;
      }
    }
    page(); lines(model.title, 'bold 25px Arial', `#${theme.ink}`, 31); y += 8;
    lines(`${model.date} · ${model.persona}`, '12px Arial', `#${theme.muted}`, 17); y += 10;
    lines('Локальный макет. Анализ файлов и работа агентов не выполнялись.', 'italic 11px Arial', `#${theme.muted}`, 16); y += 20;
    for (const section of model.sections) {
      if (y > 700) page();
      lines(section.title, 'bold 16px Arial', `#${theme.blue}`, 23); y += 3;
      for (const line of section.lines) { lines(line, '12px Arial', `#${theme.ink}`, 18); y += 7; }
      y += 8;
    }
    const chunks = [], offsets = [0]; let length = 0;
    const push = bytes => { chunks.push(bytes); length += bytes.length; };
    const str = text => encoder.encode(text);
    push(str('%PDF-1.4\n%\xE2\xE3\xCF\xD3\n'));
    const object = (n, parts) => { offsets[n] = length; push(str(`${n} 0 obj\n`)); for (const part of parts) push(part); push(str('\nendobj\n')); };
    object(1, [str('<< /Type /Catalog /Pages 2 0 R >>')]);
    object(2, [str(`<< /Type /Pages /Kids [${pages.map((_, i) => `${3 + i * 3} 0 R`).join(' ')}] /Count ${pages.length} >>`)]);
    for (let i = 0; i < pages.length; i++) {
      const n = 3 + i * 3;
      const base64 = pages[i].toDataURL('image/jpeg', 0.88).split(',')[1];
      const binary = atob(base64), image = new Uint8Array(binary.length);
      for (let j = 0; j < binary.length; j++) image[j] = binary.charCodeAt(j);
      const content = str('q 595 0 0 842 0 0 cm /Im1 Do Q');
      object(n, [str(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /XObject << /Im1 ${n + 1} 0 R >> >> /Contents ${n + 2} 0 R >>`)]);
      object(n + 1, [str(`<< /Type /XObject /Subtype /Image /Width 1190 /Height 1684 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${image.length} >>\nstream\n`), image, str('\nendstream')]);
      object(n + 2, [str(`<< /Length ${content.length} >>\nstream\n`), content, str('\nendstream')]);
    }
    const xref = length;
    push(str(`xref\n0 ${offsets.length}\n0000000000 65535 f \n`));
    for (let i = 1; i < offsets.length; i++) push(str(`${String(offsets[i]).padStart(10, '0')} 00000 n \n`));
    push(str(`trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`));
    return new Blob(chunks, { type: 'application/pdf' });
  }

  async function create(format, model) {
    if (format === 'pdf') return pdf(model);
    if (format === 'docx') return docx(model);
    if (format === 'pptx') return pptx(model);
    if (format === 'xlsx') return xlsx(model);
    if (format === 'md') return new Blob([model.markdown], { type: 'text/markdown;charset=utf-8' });
    throw new Error(`Неизвестный формат: ${format}`);
  }
  globalThis.CoreTeamsExport = { create, theme };
})();
