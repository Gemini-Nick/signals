const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, HeadingLevel, BorderStyle, WidthType,
  ShadingType, PageNumber, PageBreak, LevelFormat
} = require("docx");

// ── Shared styles ──
const FONT = "Arial";
const styles = {
  default: { document: { run: { font: FONT, size: 22 } } },
  paragraphStyles: [
    {
      id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
      run: { size: 36, bold: true, font: FONT, color: "1A1A2E" },
      paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 }
    },
    {
      id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
      run: { size: 30, bold: true, font: FONT, color: "16213E" },
      paragraph: { spacing: { before: 280, after: 160 }, outlineLevel: 1 }
    },
    {
      id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
      run: { size: 26, bold: true, font: FONT, color: "0F3460" },
      paragraph: { spacing: { before: 200, after: 120 }, outlineLevel: 2 }
    },
  ]
};

const numbering = {
  config: [
    {
      reference: "bullets",
      levels: [{
        level: 0, format: LevelFormat.BULLET, text: "\u2022", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } }
      }]
    },
    {
      reference: "bullets2",
      levels: [{
        level: 0, format: LevelFormat.BULLET, text: "\u2022", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 1080, hanging: 360 } } }
      }]
    },
    {
      reference: "numbers",
      levels: [{
        level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } }
      }]
    },
  ]
};

const border = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const borders = { top: border, bottom: border, left: border, right: border };
const cellMargins = { top: 60, bottom: 60, left: 100, right: 100 };

function headerCell(text, width) {
  return new TableCell({
    borders, width: { size: width, type: WidthType.DXA },
    shading: { fill: "1A1A2E", type: ShadingType.CLEAR },
    margins: cellMargins,
    children: [new Paragraph({ children: [new TextRun({ text, bold: true, color: "FFFFFF", font: FONT, size: 20 })] })]
  });
}

function cell(text, width) {
  return new TableCell({
    borders, width: { size: width, type: WidthType.DXA },
    margins: cellMargins,
    children: [new Paragraph({ children: [new TextRun({ text, font: FONT, size: 20 })] })]
  });
}

function boldCell(text, width) {
  return new TableCell({
    borders, width: { size: width, type: WidthType.DXA },
    margins: cellMargins,
    children: [new Paragraph({ children: [new TextRun({ text, bold: true, font: FONT, size: 20 })] })]
  });
}

function h1(text) { return new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(text)] }); }
function h2(text) { return new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(text)] }); }
function h3(text) { return new Paragraph({ heading: HeadingLevel.HEADING_3, children: [new TextRun(text)] }); }

function p(text) {
  return new Paragraph({ spacing: { after: 120 }, children: [new TextRun({ text, font: FONT, size: 22 })] });
}
function pBold(text) {
  return new Paragraph({ spacing: { after: 120 }, children: [new TextRun({ text, font: FONT, size: 22, bold: true })] });
}
function bullet(text) {
  return new Paragraph({ numbering: { reference: "bullets", level: 0 }, spacing: { after: 80 }, children: [new TextRun({ text, font: FONT, size: 22 })] });
}
function bullet2(text) {
  return new Paragraph({ numbering: { reference: "bullets2", level: 0 }, spacing: { after: 80 }, children: [new TextRun({ text, font: FONT, size: 22 })] });
}
function numbered(text) {
  return new Paragraph({ numbering: { reference: "numbers", level: 0 }, spacing: { after: 80 }, children: [new TextRun({ text, font: FONT, size: 22 })] });
}
function codeBlock(text) {
  return new Paragraph({
    spacing: { after: 80 },
    shading: { fill: "F5F5F5", type: ShadingType.CLEAR },
    indent: { left: 360 },
    children: [new TextRun({ text, font: "Courier New", size: 18 })]
  });
}
function pageBreak() { return new Paragraph({ children: [new PageBreak()] }); }
function spacer() { return new Paragraph({ spacing: { after: 80 }, children: [] }); }

const sectionProps = {
  page: {
    size: { width: 12240, height: 15840 },
    margin: { top: 1440, right: 1200, bottom: 1440, left: 1200 }
  }
};

// ════════════════════════════════════════════════════════════════
// Document 1: Research Findings
// ════════════════════════════════════════════════════════════════
function buildResearchDoc() {
  const children = [
    // Title page
    new Paragraph({ spacing: { before: 3000 }, alignment: AlignmentType.CENTER, children: [
      new TextRun({ text: "\u{1F40C} \u9686\u5C0F\u4FA0 LONG CLAW", font: FONT, size: 48, bold: true, color: "1A1A2E" })
    ]}),
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 }, children: [
      new TextRun({ text: "\u7CFB\u7EDF\u91CD\u6784 \u2014 \u7814\u7A76\u6210\u679C\u6C47\u603B", font: FONT, size: 32, color: "0F3460" })
    ]}),
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 600 }, children: [
      new TextRun({ text: "\u7814\u7A76\u65E5\u671F\uFF1A2026-03-07", font: FONT, size: 24, color: "666666" })
    ]}),
    new Paragraph({ alignment: AlignmentType.CENTER, children: [
      new TextRun({ text: "\u76EE\u6807\uFF1A\u4E3A\u56DB\u9636\u6BB5\u4EA4\u6613\u5FAA\u73AF\u67B6\u6784\u63D0\u4F9B\u7406\u8BBA\u57FA\u7840", font: FONT, size: 22, color: "999999" })
    ]}),
    pageBreak(),

    // ── 1. Professional trader workflow ──
    h1("1. \u804C\u4E1A\u4EA4\u6613\u5458\u65E5\u5E38\u5DE5\u4F5C\u6D41"),
    h2("\u7814\u7A76\u53D1\u73B0"),
    p("\u804C\u4E1A\u4EA4\u6613\u5458\u7684\u4E00\u5929\u4E25\u683C\u6309\u7167\u56DB\u4E2A\u9636\u6BB5\u8FD0\u4F5C\uFF0C\u6BCF\u4E2A\u9636\u6BB5\u6709\u4E0D\u540C\u7684\u601D\u7EF4\u6A21\u5F0F\u548C\u8F93\u51FA\u4EA7\u7269\uFF1A"),
    pBold("\u76D8\u524D\uFF086:00-9:15\uFF09\u2014 \u5236\u5B9A\u4F5C\u6218\u8BA1\u5212"),
    bullet("\u5BA1\u67E5\u9694\u591C\u5E02\u573A\uFF08\u7F8E\u80A1/\u6B27\u6D32/\u671F\u8D27/\u5916\u6C47\uFF09"),
    bullet("\u626B\u63CF\u65B0\u95FB\u4E8B\u4EF6\uFF08\u8D22\u62A5\u3001\u653F\u7B56\u3001\u5730\u7F18\u653F\u6CBB\uFF09"),
    bullet("\u6807\u8BB0\u5173\u952E\u4EF7\u4F4D\uFF08\u652F\u6491/\u963B\u529B/\u67A2\u8F74\u70B9\uFF09"),
    bullet("\u5236\u5B9A\u5F53\u65E5\u4EA4\u6613\u8BA1\u5212\uFF1A\u65B9\u5411\u504F\u597D + \u5177\u4F53\u5165\u573A/\u51FA\u573A\u6761\u4EF6"),
    pBold("\u76D8\u4E2D\uFF089:15-15:00\uFF09\u2014 \u6267\u884C\u4E0E\u5E94\u5BF9"),
    bullet("\u4E25\u683C\u6309\u76D8\u524D\u8BA1\u5212\u6267\u884C"),
    bullet("\u4FE1\u53F7\u9A71\u52A8\uFF1A\u53EA\u5728\u9884\u8BBE\u6761\u4EF6\u89E6\u53D1\u65F6\u884C\u52A8"),
    bullet("\u4E0D\u505A\u8BA1\u5212\u5916\u7684\u51B2\u52A8\u4EA4\u6613"),
    pBold("\u76D8\u540E\uFF0815:00-17:00\uFF09\u2014 \u590D\u76D8\u4E0E\u603B\u7ED3"),
    bullet("\u9010\u7B14\u56DE\u987E\u5F53\u65E5\u4EA4\u6613"),
    bullet("\u8BB0\u5F55\u9519\u8BEF\u5E76\u5206\u7C7B"),
    bullet("\u66F4\u65B0\u7EDF\u8BA1\u6307\u6807\uFF08\u80DC\u7387/\u76C8\u4E8F\u6BD4/\u6700\u5927\u56DE\u64A4\uFF09"),
    pBold("\u5468\u672B \u2014 \u5927\u5C40\u601D\u8003"),
    bullet("\u5468\u7EBF\u7EA7\u522B\u7ED3\u6784\u5206\u6790"),
    bullet("\u4E0B\u5468\u4E8B\u4EF6\u65E5\u5386\u5BA1\u67E5"),
    bullet("\u4ED3\u4F4D\u518D\u5E73\u8861 + \u7B56\u7565\u53C2\u6570\u5FAE\u8C03"),

    h2("\u5BF9\u7CFB\u7EDF\u8BBE\u8BA1\u7684\u542F\u793A"),
    numbered("\u56DB\u9636\u6BB5\u5FAA\u73AF\u662F\u81EA\u7136\u7684\u5DE5\u4F5C\u6D41\uFF0C\u4E0D\u662F\u4EBA\u4E3A\u5212\u5206"),
    numbered("\u6BCF\u4E2A\u9636\u6BB5\u7684\u8F93\u5165/\u8F93\u51FA/\u601D\u7EF4\u6A21\u5F0F\u5B8C\u5168\u4E0D\u540C"),
    numbered("\u76D8\u524D\u7684\u6838\u5FC3\u662F\u300C\u5B8C\u5168\u5206\u7C7B + \u5206\u6BB5\u51FD\u6570\u300D\uFF0C\u4E0D\u662F\u5355\u4E00\u9884\u6D4B"),
    numbered("\u76D8\u4E2D\u7684\u6838\u5FC3\u662F\u300C\u5E94\u5BF9\u300D\uFF0C\u4E0D\u662F\u300C\u9884\u6D4B\u300D"),
    numbered("\u76D8\u540E\u7684\u6838\u5FC3\u662F\u300C\u5F52\u56E0\u300D\uFF0C\u4E0D\u662F\u300C\u603B\u7ED3\u6DA8\u8DCC\u300D"),
    pageBreak(),

    // ── 2. Quant fund ──
    h1("2. \u91CF\u5316\u57FA\u91D1\u73AF\u5883\u5206\u79BB\u601D\u8DEF"),
    p("\u5E7B\u65B9\u91CF\u5316 / Two Sigma / Renaissance \u4E25\u683C\u5206\u79BB\u4E09\u4E2A\u73AF\u5883\uFF1A"),
    new Table({
      width: { size: 9840, type: WidthType.DXA },
      columnWidths: [2000, 2500, 2500, 2840],
      rows: [
        new TableRow({ children: [headerCell("\u73AF\u5883", 2000), headerCell("\u6570\u636E", 2500), headerCell("\u76EE\u7684", 2500), headerCell("\u9694\u79BB", 2840)] }),
        new TableRow({ children: [boldCell("\u7814\u7A76\u73AF\u5883", 2000), cell("\u5386\u53F2\u5168\u91CF", 2500), cell("\u56E0\u5B50\u6316\u6398\u3001\u7B56\u7565\u7814\u53D1", 2500), cell("\u4E0D\u53EF\u6267\u884C\u4EA4\u6613", 2840)] }),
        new TableRow({ children: [boldCell("\u56DE\u6D4B\u73AF\u5883", 2000), cell("\u5386\u53F2+\u6A21\u62DF\u64AE\u5408", 2500), cell("\u7B56\u7565\u9A8C\u8BC1\u3001\u53C2\u6570\u4F18\u5316", 2500), cell("\u4E25\u9632\u672A\u6765\u4FE1\u606F", 2840)] }),
        new TableRow({ children: [boldCell("\u751F\u4EA7\u73AF\u5883", 2000), cell("\u5B9E\u65F6\u884C\u60C5", 2500), cell("\u5B9E\u76D8\u6267\u884C", 2500), cell("\u98CE\u63A7\u9694\u79BB", 2840)] }),
      ]
    }),
    spacer(),
    p("\u5BF9\u9686\u5C0F\u4FA0\u7684\u501F\u9274\uFF1A\u4EFF\u771F\u73AF\u5883 = \u56DE\u6D4B\u73AF\u5883\uFF0C\u5FC5\u987B\u4E25\u683C\u9694\u79BB\u6570\u636E\uFF1B\u76D8\u4E2D\u76D1\u63A7 = \u751F\u4EA7\u73AF\u5883\uFF0C\u7528\u5B9E\u65F6\u6570\u636E\u3002"),
    pageBreak(),

    // ── 3. Chan Theory practitioner ──
    h1("3. \u7F20\u8BBA\u5B9E\u6218\u8005\u5DE5\u4F5C\u6D41"),
    pBold("\u76D8\u524D \u2014 \u5B8C\u5168\u5206\u7C7B"),
    bullet("\u786E\u5B9A\u64CD\u4F5C\u7EA7\u522B\uFF08\u598230\u5206\u949F\uFF09"),
    bullet("\u753B\u51FA\u5F53\u524D\u4E2D\u67A2\u4F4D\u7F6E\uFF08ZG/ZD\uFF09"),
    bullet("\u679A\u4E3E\u6240\u6709\u53EF\u80FD\u8D70\u52BF\uFF08\u4E0A/\u4E2D/\u4E0B\u4E09\u79CD\u5206\u7C7B\uFF09"),
    bullet("\u6BCF\u79CD\u5206\u7C7B\u8BBE\u5B9A\u64CD\u4F5C\u9884\u6848"),
    pBold("\u76D8\u4E2D \u2014 \u5F53\u4E0B\u5224\u65AD"),
    bullet("\u300C\u8D70\u52BF\u7EC8\u5B8C\u7F8E\u300D\uFF1A\u4EFB\u4F55\u8D70\u52BF\u7EC8\u5C06\u5B8C\u6210"),
    bullet("\u8DDF\u8E2A\u5E02\u573A\u9009\u62E9\u4E86\u54EA\u4E2A\u5206\u7C7B"),
    bullet("\u5F53\u5206\u7C7B\u786E\u8BA4\u65F6\u6267\u884C\u9884\u6848"),
    pBold("\u76D8\u540E \u2014 \u590D\u76D8\u7ED3\u6784"),
    bullet("\u753B\u56FE\uFF08\u7B14/\u7EBF\u6BB5/\u4E2D\u67A2\uFF09"),
    bullet("\u9A8C\u8BC1\u76D8\u524D\u5206\u7C7B\u662F\u5426\u8986\u76D6\u4E86\u5B9E\u9645\u8D70\u52BF"),
    bullet("\u5982\u679C\u8D70\u52BF\u8D85\u51FA\u5206\u7C7B\uFF0C\u5206\u6790\u54EA\u91CC\u5224\u65AD\u6709\u8BEF"),
    spacer(),
    p("\u7F20\u8BBA\u7B2C68\u8BFE\u6838\u5FC3\u601D\u60F3\uFF1A\u7528\u5B8C\u5168\u5206\u7C7B\u7684\u65B9\u5F0F\u5BF9\u5F85\u5E02\u573A\uFF0C\u6BCF\u79CD\u5206\u7C7B\u914D\u4E00\u4E2A\u64CD\u4F5C\uFF0C\u8FD9\u5C31\u662F\u5206\u6BB5\u51FD\u6570\u3002\u5E02\u573A\u8D70\u5230\u54EA\u4E2A\u5206\u7C7B\uFF0C\u5C31\u6267\u884C\u54EA\u4E2A\u64CD\u4F5C\u3002"),
    pageBreak(),

    // ── 4. Elliott Wave vs Chan Theory ──
    h1("4. \u6CE2\u6D6A\u7406\u8BBA\u4E0E\u7F20\u8BBA\u5BF9\u6BD4"),
    h2("\u6CE2\u6D6A\u7406\u8BBA\u6838\u5FC3"),
    bullet("\u63A8\u52A8\u6D6A\uFF1A5\u6D6A\u7ED3\u6784\uFF081-2-3-4-5\uFF09"),
    bullet("\u8C03\u6574\u6D6A\uFF1A3\u6D6A\u7ED3\u6784\uFF08A-B-C\uFF09"),
    bullet("\u6D6A\u4E2D\u6709\u6D6A\uFF1A\u5206\u5F62\u7ED3\u6784\uFF08fractal\uFF09"),
    pBold("\u4E09\u5927\u94C1\u5F8B\uFF1A"),
    numbered("\u6D6A2\u4E0D\u80FD\u8DCC\u7834\u6D6A1\u8D77\u70B9"),
    numbered("\u6D6A3\u4E0D\u80FD\u662F\u6700\u77ED\u7684\u63A8\u52A8\u6D6A"),
    numbered("\u6D6A4\u4E0D\u80FD\u4E0E\u6D6A1\u91CD\u53E0"),
    h2("\u7F20\u8BBA\u5BF9\u6CE2\u6D6A\u7406\u8BBA\u7684\u6279\u8BC4"),
    pBold("\u300C\u5343\u4EBA\u5343\u6D6A\u300D\u95EE\u9898\uFF1A"),
    bullet("\u6CE2\u6D6A\u7406\u8BBA\u7684\u6700\u5927\u5F31\u70B9\u662F\u4E3B\u89C2\u6027\uFF1A\u540C\u4E00\u6BB5\u8D70\u52BF\uFF0C\u4E0D\u540C\u4EBA\u6570\u51FA\u4E0D\u540C\u7684\u6D6A\u5F62"),
    bullet("\u6CA1\u6709\u4E25\u683C\u7684\u6570\u5B66\u5B9A\u4E49\uFF0C\u6D6A\u7684\u5212\u5206\u4F9D\u8D56\u7ECF\u9A8C"),
    bullet("\u300C\u5931\u8D25\u6D6A\u300D\u300C\u5EF6\u4F38\u6D6A\u300D\u7B49\u6982\u5FF5\u4F7F\u5F97\u7406\u8BBA\u51E0\u4E4E\u4E0D\u53EF\u8BC1\u4F2A"),
    h2("\u53EF\u501F\u9274\u4E4B\u5904"),
    numbered("\u659C\u6CE2\u90A3\u5951\u6BD4\u4F8B\uFF1A\u7B14\u7684\u5EF6\u4F38\u76EE\u6807\u4F4D\u53EF\u7528 0.618/1.618 \u8BA1\u7B97"),
    numbered("\u8C03\u6574\u6D6A\u5F62\u6001\uFF1AA-B-C \u4E0E\u7F20\u8BBA\u4E2D\u67A2\u56DE\u8C03\u7ED3\u6784\u6709\u5BF9\u5E94"),
    numbered("\u5206\u5F62\u601D\u7EF4\uFF1A\u81EA\u76F8\u4F3C\u6027\u4E0E\u7F20\u8BBA\u591A\u7EA7\u522B\u9012\u5F52\u4E00\u81F4"),
    pageBreak(),

    // ── 5. Three Kisses ──
    h1("5. \u7F20\u8BBA\u5747\u7EBF\u7CFB\u7EDF \u2014 \u4E09\u79CD\u543B"),
    new Table({
      width: { size: 9840, type: WidthType.DXA },
      columnWidths: [1500, 3000, 2500, 2840],
      rows: [
        new TableRow({ children: [headerCell("\u7C7B\u578B", 1500), headerCell("\u5B9A\u4E49", 3000), headerCell("\u542B\u4E49", 2500), headerCell("\u7EA7\u522B\u63D0\u793A", 2840)] }),
        new TableRow({ children: [boldCell("\u98DE\u543B", 1500), cell("\u77ED\u671F\u5747\u7EBF\u9760\u8FD1\u957F\u671F\u5747\u7EBF\u4F46\u4E0D\u63A5\u89E6\u5373\u5F39\u5F00", 3000), cell("\u8D8B\u52BF\u6781\u5F3A\uFF0C\u56DE\u8C03\u6781\u6D45", 2500), cell("\u6B21\u7EA7\u522B\u56DE\u8C03\u4E0D\u5145\u5206", 2840)] }),
        new TableRow({ children: [boldCell("\u5507\u543B", 1500), cell("\u77ED\u671F\u5747\u7EBF\u63A5\u89E6\u957F\u671F\u5747\u7EBF\u540E\u5F39\u5F00", 3000), cell("\u6B63\u5E38\u56DE\u8C03\uFF0C\u8D8B\u52BF\u5EF6\u7EED", 2500), cell("\u6B21\u7EA7\u522B\u56DE\u8C03\u5145\u5206", 2840)] }),
        new TableRow({ children: [boldCell("\u6E7F\u543B", 1500), cell("\u77ED\u671F\u5747\u7EBF\u7A7F\u8D8A\u957F\u671F\u5747\u7EBF\u540E\u7EA0\u7F20", 3000), cell("\u53EF\u80FD\u53CD\u8F6C\u533A\u57DF", 2500), cell("\u6B21\u7EA7\u522B\u53EF\u80FD\u5F62\u6210\u4E2D\u67A2", 2840)] }),
      ]
    }),
    spacer(),
    p("\u5747\u7EBF\u7684\u300C\u543B\u300D\u53CD\u6620\u7684\u662F\u8D8B\u52BF\u7684\u529B\u5EA6\u548C\u8010\u529B\u3002\u98DE\u543B\u2192\u5507\u543B\u2192\u6E7F\u543B\u7684\u6F14\u53D8\u662F\u8D8B\u52BF\u529B\u5EA6\u8870\u51CF\u7684\u9884\u8B66\u3002"),
    pageBreak(),

    // ── 6. MACD Dynamics ──
    h1("6. MACD \u52A8\u529B\u5B66 \u2014 \u9762\u79EF\u6BD4\u8F83\u6CD5"),
    p("MACD \u4E0D\u662F\u7B80\u5355\u770B\u91D1\u53C9\u6B7B\u53C9\uFF0C\u800C\u662F\u7528\u9762\u79EF\u6BD4\u8F83\u6765\u5224\u65AD\u8D8B\u52BF\u529B\u5EA6\uFF1A"),
    pBold("A-B-C \u4E09\u6BB5\u6846\u67B6\uFF1A"),
    bullet("\u4EF7\u683C: A\u6BB5\uFF08\u7B2C\u4E00\u6BB5\u4E0A\u6DA8\uFF09\u2192 B\u6BB5\uFF08\u56DE\u8C03\uFF09\u2192 C\u6BB5\uFF08\u7B2C\u4E8C\u6BB5\u4E0A\u6DA8\uFF09"),
    bullet("MACD: \u9762\u79EFA\uFF08\u7EA2\u67F1\u533A\u57DF\uFF09\u2192 \u9762\u79EFB\uFF08\u7EFF\u67F1\uFF09\u2192 \u9762\u79EFC\uFF08\u7EA2\u67F1\u533A\u57DF\uFF09"),
    bullet("\u5224\u65AD: \u82E5\u9762\u79EFC < \u9762\u79EFA \u2192 \u9876\u80CC\u9A70 \u2192 \u5356\u51FA\u4FE1\u53F7"),
    pBold("\u80CC\u9A70\u529B\u5EA6\u516C\u5F0F\uFF1A"),
    codeBlock("\u80CC\u9A70\u529B\u5EA6 = MACD\u9762\u79EF(\u5F53\u524D\u6BB5) / MACD\u9762\u79EF(\u524D\u4E00\u540C\u5411\u6BB5)"),
    bullet("> 1.0: \u529B\u5EA6\u589E\u5F3A\uFF08\u975E\u80CC\u9A70\uFF09"),
    bullet("0.5~1.0: \u8F7B\u5FAE\u80CC\u9A70"),
    bullet("< 0.5: \u660E\u663E\u80CC\u9A70\uFF08\u9AD8\u6982\u7387\u53CD\u8F6C\uFF09"),
    pBold("\u9EC4\u767D\u7EBF\u56DE\u96F6\u8F74\uFF1A"),
    bullet("DIF/DEA \u56DE\u5230\u96F6\u8F74\u9644\u8FD1 = \u4E2D\u67A2\u9707\u8361"),
    bullet("\u53CC\u56DE\u8BD5\uFF1A\u9EC4\u767D\u7EBF\u4E24\u6B21\u56DE\u5230\u96F6\u8F74 = \u4E24\u4E2A\u4E2D\u67A2\u5F62\u6210\uFF0C\u53EF\u80FD\u8D8B\u52BF\u53CD\u8F6C"),
    pageBreak(),

    // ── 7. Dow Theory ──
    h1("7. \u9053\u6C0F\u7406\u8BBA\u4E0E\u7F20\u8BBA\u5BF9\u6BD4"),
    new Table({
      width: { size: 9840, type: WidthType.DXA },
      columnWidths: [2000, 2500, 2500, 2840],
      rows: [
        new TableRow({ children: [headerCell("\u6982\u5FF5", 2000), headerCell("\u9053\u6C0F\u7406\u8BBA", 2500), headerCell("\u7F20\u8BBA", 2500), headerCell("\u6539\u8FDB", 2840)] }),
        new TableRow({ children: [boldCell("\u8D8B\u52BF\u5B9A\u4E49", 2000), cell("\u66F4\u9AD8\u7684\u9AD8\u70B9/\u4F4E\u70B9", 2500), cell("\u7EBF\u6BB5/\u7B14\u7684\u65B9\u5411", 2500), cell("\u6570\u5B66\u7CBE\u786E", 2840)] }),
        new TableRow({ children: [boldCell("\u8D8B\u52BF\u7EA7\u522B", 2000), cell("\u4E3B\u8981/\u6B21\u8981/\u77ED\u6682", 2500), cell("1F/5F/30F/\u65E5/\u5468", 2500), cell("\u53EF\u9012\u5F52\u5B9A\u4E49", 2840)] }),
        new TableRow({ children: [boldCell("\u53CD\u8F6C\u4FE1\u53F7", 2000), cell("\u9AD8\u70B9/\u4F4E\u70B9\u88AB\u7A81\u7834", 2500), cell("\u4E2D\u67A2+\u80CC\u9A70+\u4E70\u5356\u70B9", 2500), cell("\u591A\u91CD\u786E\u8BA4", 2840)] }),
        new TableRow({ children: [boldCell("\u5165\u573A\u65F6\u673A", 2000), cell("\u786E\u8BA4\u540E\u4E70\u5165\uFF08\u6EDE\u540E\uFF09", 2500), cell("\u533A\u95F4\u5957\u7CBE\u786E\u5B9A\u4F4D", 2500), cell("\u7CBE\u5EA6\u66F4\u9AD8", 2840)] }),
      ]
    }),
    spacer(),
    p("\u53EF\u501F\u9274\uFF1A\u6307\u6570\u76F8\u4E92\u786E\u8BA4\u539F\u5219\u5DF2\u4F53\u73B0\u5728 Layer 1\uFF0C\u6210\u4EA4\u91CF\u786E\u8BA4\u53EF\u4EE5\u52A0\u5F3A\u3002"),
    pageBreak(),

    // ── 8. Eastern Philosophy ──
    h1("8. \u4E1C\u65B9\u54F2\u5B66\u57FA\u7840"),
    p("\u7F20\u8BBA\u521B\u59CB\u4EBA\u5728108\u8BFE\u4E2D\u9891\u7E41\u5F15\u7528\u4E09\u5927\u7ECF\u5178\uFF0C\u8FD9\u4E9B\u4E0D\u662F\u88C5\u9970\u800C\u662F\u5206\u6790\u54F2\u5B66\u7684\u6839\u57FA\uFF1A"),
    new Table({
      width: { size: 9840, type: WidthType.DXA },
      columnWidths: [2200, 2800, 2300, 2540],
      rows: [
        new TableRow({ children: [headerCell("\u54F2\u5B66\u539F\u5219", 2200), headerCell("\u7F20\u8BBA\u4F53\u73B0", 2800), headerCell("\u7CFB\u7EDF\u5B9E\u73B0", 2300), headerCell("\u6765\u6E90", 2540)] }),
        new TableRow({ children: [boldCell("\u53CD\u8005\u9053\u4E4B\u52A8", 2200), cell("\u80CC\u9A70\u5FC5\u53CD\u8F6C", 2800), cell("\u80CC\u9A70\u68C0\u6D4B+\u9006\u5411\u60C5\u7EEA", 2300), cell("\u300A\u9053\u5FB7\u7ECF\u300B", 2540)] }),
        new TableRow({ children: [boldCell("\u5E94\u65E0\u6240\u4F4F", 2200), cell("\u5B8C\u5168\u5206\u7C7B", 2800), cell("\u5206\u6BB5\u51FD\u6570\u5F0F\u8BA1\u5212", 2300), cell("\u300A\u91D1\u521A\u7ECF\u300B", 2540)] }),
        new TableRow({ children: [boldCell("\u77E5\u4E4B\u4E3A\u77E5\u4E4B", 2200), cell("\u786E\u5B9A\u6027\u4F18\u5148", 2800), cell("\u4FE1\u53F7\u7F6E\u4FE1\u5EA6\u8BC4\u5206", 2300), cell("\u300A\u8BBA\u8BED\u300B", 2540)] }),
        new TableRow({ children: [boldCell("\u7269\u6781\u5FC5\u53CD", 2200), cell("\u4E09\u4E70\u4E09\u5356", 2800), cell("\u4E2D\u67A2\u7A81\u7834\u540E\u8870\u7AED\u68C0\u6D4B", 2300), cell("\u300A\u9053\u5FB7\u7ECF\u300B", 2540)] }),
      ]
    }),
    pageBreak(),

    // ── 9. Target Price Calculation ──
    h1("9. \u76D8\u524D\u8BA1\u5212 \u2014 \u76EE\u6807\u4F4D\u8BA1\u7B97\u65B9\u6CD5"),
    h2("\u65B9\u6CD5\u4E00\uFF1A\u4E2D\u67A2\u8FB9\u754C\u6CD5\uFF08\u6700\u57FA\u7840\uFF09"),
    bullet("\u4E2D\u67A2 = \u4E09\u7B14\u91CD\u53E0\u533A\u95F4"),
    bullet("ZG = \u4E2D\u67A2\u4E0A\u6CBF = min(\u7B2C2\u7B14\u9AD8\u70B9, \u7B2C3\u7B14\u9AD8\u70B9)"),
    bullet("ZD = \u4E2D\u67A2\u4E0B\u6CBF = max(\u7B2C1\u7B14\u4F4E\u70B9, \u7B2C2\u7B14\u4F4E\u70B9)"),
    h2("\u65B9\u6CD5\u4E8C\uFF1A\u533A\u95F4\u5957\u7CBE\u5EA6\u6CD5\uFF08\u6838\u5FC3\u65B9\u6CD5\uFF09"),
    bullet("\u5927\u7EA7\u522B\u80CC\u9A70\u6BB5\u5B9A\u4F4D \u2192 \u6B21\u7EA7\u522B\u7CBE\u786E\u5B9A\u4F4D \u2192 \u66F4\u5C0F\u7EA7\u522B\u7CBE\u786E\u533A\u95F4"),
    bullet("\u7F20\u8BBA\u6700\u6838\u5FC3\u7684\u7CBE\u786E\u5B9A\u4F4D\u65B9\u6CD5"),
    bullet("\u7CBE\u5EA6\u53EF\u4EE5\u5230\u5177\u4F53\u4EF7\u683C\u00B11%"),
    h2("\u65B9\u6CD5\u4E09\uFF1A\u65B0\u4E2D\u67A2\u5F62\u6210\u76EE\u6807\u6CD5"),
    bullet("\u4E0A\u6DA8\u8D8B\u52BF\u4E2D\uFF0C\u65B0\u4E2D\u67A2\u901A\u5E38\u5728\u524D\u4E00\u4E2D\u67A2ZG\u4E0A\u65B9"),
    bullet("\u65B0\u4E2D\u67A2\u7684\u9AD8\u5EA6 \u2248 \u524D\u4E00\u4E2D\u67A2\u9AD8\u5EA6\u7684 0.618-1.0 \u500D"),
    h2("\u65B9\u6CD5\u56DB\uFF1A\u659C\u6CE2\u90A3\u5951\u5EF6\u4F38\u6CD5\uFF08\u8F85\u52A9\uFF09"),
    bullet("\u7B14\u7684\u7B49\u957F\u6295\u5C04 / 0.618 / 1.618 \u5EF6\u4F38"),
    bullet("\u591A\u79CD\u65B9\u6CD5\u5728\u540C\u4E00\u533A\u57DF\u6C47\u805A = \u5F3A\u652F\u6491/\u963B\u529B"),
    pageBreak(),

    // ── 10. Error Classification ──
    h1("10. \u76D8\u540E\u590D\u76D8 \u2014 \u9519\u8BEF\u5206\u7C7B\u6846\u67B6"),
    h2("Type A \u2014 \u7CFB\u7EDF\u65B9\u5DEE (System Variance)"),
    bullet("\u5B9A\u4E49\uFF1A\u4E25\u683C\u6267\u884C\u7B56\u7565\uFF0C\u4F46\u4ECD\u7136\u4E8F\u635F"),
    bullet("\u5904\u7406\uFF1A\u63A5\u53D7\uFF0C\u4E0D\u4FEE\u6539\u7B56\u7565"),
    bullet("\u5360\u6BD4\u76EE\u6807\uFF1A\u226560%"),
    h2("Type B \u2014 \u6267\u884C\u5931\u8BEF (Execution Error)"),
    bullet("\u5B9A\u4E49\uFF1A\u4FE1\u53F7\u6B63\u786E\uFF0C\u4F46\u6267\u884C\u6709\u504F\u5DEE"),
    bullet("\u5E38\u89C1\uFF1A\u79FB\u9664\u6B62\u635F\u3001\u4ED3\u4F4D\u8FC7\u91CD\u3001\u63D0\u524D\u5165\u573A\u3001\u5EF6\u8FDF\u51FA\u573A"),
    bullet("\u5360\u6BD4\u76EE\u6807\uFF1A\u226430%"),
    h2("Type C \u2014 \u60C5\u7EEA\u4EA4\u6613 (Emotional Trade)"),
    bullet("\u5B9A\u4E49\uFF1A\u5B8C\u5168\u4E0D\u5728\u8BA1\u5212\u5185\u7684\u4EA4\u6613"),
    bullet("\u5E38\u89C1\uFF1AFOMO\u8FFD\u6DA8\u3001\u62A5\u590D\u6027\u52A0\u4ED3\u3001\u542C\u6D88\u606F\u4E70\u5165"),
    bullet("\u5360\u6BD4\u76EE\u6807\uFF1A\u226410%"),
    h2("\u9057\u6F0F\u5206\u6790 (Omission Analysis)"),
    bullet("\u9057\u6F0F = \u7CFB\u7EDF\u53D1\u51FA\u4FE1\u53F7 + \u6EE1\u8DB3\u5165\u573A\u6761\u4EF6 + \u672A\u6267\u884C"),
    bullet("\u76EE\u6807\u9057\u6F0F\u7387: < 20%"),
    h2("\u6267\u884C\u8BC4\u5206\uFF081-5\u5206\u5236\uFF09"),
    new Table({
      width: { size: 9840, type: WidthType.DXA },
      columnWidths: [1500, 8340],
      rows: [
        new TableRow({ children: [headerCell("\u5206\u6570", 1500), headerCell("\u542B\u4E49", 8340)] }),
        new TableRow({ children: [boldCell("5", 1500), cell("\u5B8C\u7F8E\u6267\u884C\uFF1A\u4FE1\u53F7-\u5165\u573A-\u4ED3\u4F4D-\u6B62\u76C8/\u6B62\u635F\u5168\u90E8\u6B63\u786E", 8340)] }),
        new TableRow({ children: [boldCell("4", 1500), cell("\u57FA\u672C\u6B63\u786E\uFF1A\u5C0F\u504F\u5DEE\uFF08\u5165\u573A\u7565\u65E9/\u665A1-2\u6839K\u7EBF\uFF09", 8340)] }),
        new TableRow({ children: [boldCell("3", 1500), cell("\u6709\u7455\u75B5\uFF1A\u4ED3\u4F4D\u4E0D\u5F53\u6216\u6B62\u635F\u8BBE\u7F6E\u4E0D\u5408\u7406", 8340)] }),
        new TableRow({ children: [boldCell("2", 1500), cell("\u660E\u663E\u5931\u8BEF\uFF1A\u6267\u884C\u4E0E\u8BA1\u5212\u4E25\u91CD\u504F\u79BB", 8340)] }),
        new TableRow({ children: [boldCell("1", 1500), cell("\u4E25\u91CD\u9519\u8BEF\uFF1A\u5B8C\u5168\u8131\u79BB\u7CFB\u7EDF\u7684\u64CD\u4F5C", 8340)] }),
      ]
    }),
    pageBreak(),

    // ── 11. Weekend Strategy ──
    h1("11. \u5468\u672B\u7B56\u7565 \u2014 \u5927\u4E8B\u4EF6\u9884\u5224"),
    p("\u4E13\u4E1A\u4EA4\u6613\u5458\u7684\u5468\u672B\u5DE5\u4F5C\u4E0D\u4EC5\u4EC5\u662F\u770B\u5468\u7EBF\u56FE\u3002\u6838\u5FC3\u662F\u4E8B\u4EF6\u9A71\u52A8\u7684\u9884\u5224\uFF1A"),
    h2("A\u80A1\u91CD\u8981\u4E8B\u4EF6\u65E5\u5386"),
    new Table({
      width: { size: 9840, type: WidthType.DXA },
      columnWidths: [2000, 2800, 2200, 2840],
      rows: [
        new TableRow({ children: [headerCell("\u7C7B\u578B", 2000), headerCell("\u4E8B\u4EF6", 2800), headerCell("\u9891\u7387", 2200), headerCell("\u5F71\u54CD", 2840)] }),
        new TableRow({ children: [cell("\u8D27\u5E01\u653F\u7B56", 2000), cell("LPR\u62A5\u4EF7", 2800), cell("\u6BCF\u670820\u65E5", 2200), cell("\u94F6\u884C/\u5730\u4EA7", 2840)] }),
        new TableRow({ children: [cell("\u7ECF\u6D4E\u6570\u636E", 2000), cell("CPI/PPI", 2800), cell("\u6BCF\u67089-12\u65E5", 2200), cell("\u5168\u5E02\u573A", 2840)] }),
        new TableRow({ children: [cell("\u7ECF\u6D4E\u6570\u636E", 2000), cell("PMI", 2800), cell("\u6BCF\u67081\u65E5", 2200), cell("\u5468\u671F\u80A1", 2840)] }),
        new TableRow({ children: [cell("\u653F\u6CBB\u4F1A\u8BAE", 2000), cell("\u4E24\u4F1A", 2800), cell("\u6BCF\u5E743\u6708", 2200), cell("\u653F\u7B56\u4E3B\u9898", 2840)] }),
        new TableRow({ children: [cell("\u653F\u6CBB\u4F1A\u8BAE", 2000), cell("\u653F\u6CBB\u5C40\u4F1A\u8BAE", 2800), cell("\u6BCF\u5B63\u5EA6\u672B", 2200), cell("\u653F\u7B56\u65B9\u5411", 2840)] }),
      ]
    }),
    spacer(),
    h2("\u7F20\u8BBA\u7ED3\u6784 x \u4E8B\u4EF6\u53E0\u52A0"),
    pBold("\u4E8B\u4EF6\u4E0D\u521B\u9020\u8D8B\u52BF\uFF0C\u4F46\u53EF\u4EE5\u6210\u4E3A\u7ED3\u6784\u7A81\u7834\u7684\u50AC\u5316\u5242\uFF1A"),
    bullet("\u573A\u666F1: \u5E02\u573A\u5728\u4E2D\u67A2\u9707\u8361 + \u91CD\u5927\u4E8B\u4EF6 \u2192 \u4E8B\u4EF6\u53EF\u80FD\u662F\u7A81\u7834\u50AC\u5316\u5242"),
    bullet("\u573A\u666F2: \u5E02\u573A\u5728\u8D8B\u52BF\u5EF6\u7EED + \u65E0\u91CD\u5927\u4E8B\u4EF6 \u2192 \u8D8B\u52BF\u5927\u6982\u7387\u5EF6\u7EED"),
    bullet("\u573A\u666F3: \u5E02\u573A\u5728\u80CC\u9A70\u672B\u7AEF + \u91CD\u5927\u4E8B\u4EF6 \u2192 \u4E8B\u4EF6\u53EF\u80FD\u52A0\u901F\u53CD\u8F6C"),
    pageBreak(),

    // ── Summary ──
    h1("\u603B\u7ED3\uFF1A\u7814\u7A76\u6210\u679C\u5BF9\u7CFB\u7EDF\u8BBE\u8BA1\u7684\u6838\u5FC3\u6307\u5BFC"),
    new Table({
      width: { size: 9840, type: WidthType.DXA },
      columnWidths: [2500, 3500, 3840],
      rows: [
        new TableRow({ children: [headerCell("\u7814\u7A76\u4E3B\u9898", 2500), headerCell("\u6838\u5FC3\u53D1\u73B0", 3500), headerCell("\u7CFB\u7EDF\u8BBE\u8BA1\u6620\u5C04", 3840)] }),
        new TableRow({ children: [cell("\u804C\u4E1A\u4EA4\u6613\u5458\u5DE5\u4F5C\u6D41", 2500), cell("\u56DB\u9636\u6BB5\u5FAA\u73AF\u662F\u81EA\u7136\u7684", 3500), cell("CLI\u56DB\u547D\u4EE4: plan/monitor/review/weekly", 3840)] }),
        new TableRow({ children: [cell("\u91CF\u5316\u57FA\u91D1\u73AF\u5883\u5206\u79BB", 2500), cell("\u7814\u7A76/\u56DE\u6D4B/\u751F\u4EA7\u5FC5\u987B\u9694\u79BB", 3500), cell("SimDataSource + \u5B9E\u65F6\u6570\u636E\u5206\u79BB", 3840)] }),
        new TableRow({ children: [cell("\u7F20\u8BBA\u5B9E\u6218\u8005", 2500), cell("\u5B8C\u5168\u5206\u7C7B + \u5206\u6BB5\u51FD\u6570", 3500), cell("\u76D8\u524D\u8BA1\u5212\u8F93\u51FA\u683C\u5F0F", 3840)] }),
        new TableRow({ children: [cell("\u6CE2\u6D6A\u7406\u8BBA", 2500), cell("\u659C\u6CE2\u90A3\u5951\u6BD4\u4F8B\u53EF\u7528", 3500), cell("\u76EE\u6807\u4F4D\u8BA1\u7B97\u8F85\u52A9\u65B9\u6CD5", 3840)] }),
        new TableRow({ children: [cell("\u5747\u7EBF\u4E09\u79CD\u543B", 2500), cell("\u8D8B\u52BF\u529B\u5EA6\u8870\u51CF\u9884\u8B66", 3500), cell("\u4FE1\u53F7\u786E\u8BA4\u8F85\u52A9\u6307\u6807", 3840)] }),
        new TableRow({ children: [cell("MACD\u9762\u79EF\u6BD4\u8F83\u6CD5", 2500), cell("\u91CF\u5316\u80CC\u9A70\u529B\u5EA6", 3500), cell("\u80CC\u9A70\u68C0\u6D4B\u7B2C\u4E09\u91CD\u786E\u8BA4", 3840)] }),
        new TableRow({ children: [cell("\u76EE\u6807\u4F4D\u8BA1\u7B97", 2500), cell("\u56DB\u79CD\u65B9\u6CD5\u4EA4\u53C9\u9A8C\u8BC1", 3500), cell("TargetCalculator \u7C7B", 3840)] }),
        new TableRow({ children: [cell("\u9519\u8BEF\u5206\u7C7B", 2500), cell("A/B/C\u4E09\u7C7B + \u9057\u6F0F\u5206\u6790", 3500), cell("TradeReview \u6570\u636E\u7ED3\u6784", 3840)] }),
        new TableRow({ children: [cell("\u4E8B\u4EF6\u9884\u5224", 2500), cell("\u4E8B\u4EF6x\u7ED3\u6784\u53E0\u52A0", 3500), cell("\u7ECF\u6D4E\u65E5\u5386 + \u9884\u6848\u6A21\u677F", 3840)] }),
      ]
    }),
  ];

  return new Document({
    styles, numbering,
    sections: [{
      properties: {
        ...sectionProps,
      },
      headers: {
        default: new Header({ children: [new Paragraph({
          alignment: AlignmentType.RIGHT,
          children: [new TextRun({ text: "\u9686\u5C0F\u4FA0 \u7814\u7A76\u6210\u679C\u6C47\u603B", font: FONT, size: 18, color: "999999" })]
        })] })
      },
      footers: {
        default: new Footer({ children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [new TextRun({ text: "\u7B2C ", font: FONT, size: 18, color: "999999" }), new TextRun({ children: [PageNumber.CURRENT], font: FONT, size: 18, color: "999999" }), new TextRun({ text: " \u9875", font: FONT, size: 18, color: "999999" })]
        })] })
      },
      children
    }]
  });
}

// ════════════════════════════════════════════════════════════════
// Document 2: Architecture Plan
// ════════════════════════════════════════════════════════════════
function buildArchDoc() {
  const children = [
    // Title page
    new Paragraph({ spacing: { before: 3000 }, alignment: AlignmentType.CENTER, children: [
      new TextRun({ text: "\u{1F40C} \u9686\u5C0F\u4FA0 LONG CLAW", font: FONT, size: 48, bold: true, color: "1A1A2E" })
    ]}),
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 }, children: [
      new TextRun({ text: "\u7CFB\u7EDF\u67B6\u6784\u91CD\u6784\uFF1A\u56DB\u9636\u6BB5\u4EA4\u6613\u5FAA\u73AF", font: FONT, size: 32, color: "0F3460" })
    ]}),
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 }, children: [
      new TextRun({ text: "v1.0 | 2026-03-07", font: FONT, size: 24, color: "666666" })
    ]}),
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 600 }, children: [
      new TextRun({ text: "\u72B6\u6001\uFF1A\u5F85\u5BA1\u6279 \u2014 \u7B49\u5F85\u7528\u6237\u786E\u8BA4\u540E\u542F\u52A8\u5B9E\u65BD", font: FONT, size: 22, color: "E74C3C", bold: true })
    ]}),
    pageBreak(),

    // ── Overview ──
    h1("\u4E00\u3001\u56DB\u9636\u6BB5\u4EA4\u6613\u5FAA\u73AF\u603B\u89C8"),
    p("\u4EA4\u6613\u662F\u4E00\u4E2A\u56DB\u9636\u6BB5\u5FAA\u73AF\uFF0C\u6BCF\u4E2A\u9636\u6BB5\u7684\u8F93\u5165/\u8F93\u51FA/\u601D\u7EF4\u65B9\u5F0F\u5B8C\u5168\u4E0D\u540C\uFF1A"),
    codeBlock("\u5468\u672B\u7B56\u7565(\u9884\u5224\u5927\u5C40) \u2192 \u76D8\u524D\u8BA1\u5212(\u9884\u5224\u65B9\u5411) \u2192 \u76D8\u4E2D\u76D1\u63A7(\u5E94\u5BF9\u4FE1\u53F7) \u2192 \u76D8\u540E\u590D\u76D8(\u56DE\u987E\u9519\u8BEF) \u2192 \u5FAA\u73AF"),
    spacer(),
    new Table({
      width: { size: 9840, type: WidthType.DXA },
      columnWidths: [1800, 3000, 5040],
      rows: [
        new TableRow({ children: [headerCell("\u9636\u6BB5", 1800), headerCell("\u672C\u8D28", 3000), headerCell("\u7F20\u8BBA\u539F\u6587\u5BF9\u5E94", 5040)] }),
        new TableRow({ children: [boldCell("\u5468\u672B\u7B56\u7565", 1800), cell("\u9884\u5224 \u2014 \u5927\u4E8B\u4EF6\u4F1A\u600E\u4E48\u5F71\u54CD\u5E02\u573A\uFF1F", 3000), cell("\u300C\u4E0D\u6D4B\u800C\u6D4B\u300D\uFF1A\u679A\u4E3E\u6240\u6709\u53EF\u80FD", 5040)] }),
        new TableRow({ children: [boldCell("\u76D8\u524D\u8BA1\u5212", 1800), cell("\u9884\u5224 \u2014 \u4ECA\u5929\u4F1A\u8D70\u54EA\u4E2A\u5206\u7C7B\uFF1F", 3000), cell("\u7B2C68\u8BFE\u300C\u5B8C\u5168\u5206\u7C7B + \u5206\u6BB5\u51FD\u6570\u300D", 5040)] }),
        new TableRow({ children: [boldCell("\u76D8\u4E2D\u76D1\u63A7", 1800), cell("\u5E94\u5BF9 \u2014 \u5E02\u573A\u9009\u62E9\u4E86\u54EA\u4E2A\u5206\u7C7B\uFF1F", 3000), cell("\u300C\u5F53\u4E0B\u5224\u65AD\uFF0C\u4E0D\u9884\u6D4B\u300D", 5040)] }),
        new TableRow({ children: [boldCell("\u76D8\u540E\u590D\u76D8", 1800), cell("\u56DE\u987E \u2014 \u54EA\u91CC\u505A\u9519\u4E86\uFF1F", 3000), cell("\u300C\u590D\u76D8\u662F\u627E\u5230\u6BCF\u4E2A\u64CD\u4F5C\u51B3\u7B56\u7684\u4F9D\u636E\u300D", 5040)] }),
      ]
    }),
    pageBreak(),

    // ── Weekly ──
    h1("\u4E8C\u3001\u5468\u672B\u7B56\u7565 weekly"),
    pBold("\u7528\u6237\u95EE\u9898\uFF1A\u300C\u4E0B\u5468\u6709\u4EC0\u4E48\u5927\u4E8B\uFF1F\u4F1A\u600E\u4E48\u5F71\u54CD\u5E02\u573A\uFF1F\u300D"),
    bullet("\u62C9\u53D6\u4E0B\u5468\u7ECF\u6D4E\u65E5\u5386\uFF08CPI/PMI/FOMC/\u4E24\u4F1A/\u56FD\u5E38\u4F1A\u7B49\uFF09"),
    bullet("\u6BCF\u4E2A\u91CD\u5927\u4E8B\u4EF6\u5236\u4F5C\u9884\u6848\uFF08\u9E3D\u6D3E/\u9E70\u6D3E/\u4E2D\u6027 \u2192 \u5BF9\u5E94\u64CD\u4F5C\uFF09"),
    bullet("\u7F20\u8BBA\u7ED3\u6784 x \u4E8B\u4EF6\u53E0\u52A0\uFF1A\u4E8B\u4EF6\u53EF\u80FD\u6210\u4E3A\u7ED3\u6784\u7834\u4F4D\u7684\u50AC\u5316\u5242"),
    bullet("\u8F93\u51FA\u4ED3\u4F4D\u5EFA\u8BAE\u548C\u884C\u4E1A\u914D\u7F6E"),
    pageBreak(),

    // ── Plan ──
    h1("\u4E09\u3001\u76D8\u524D\u8BA1\u5212 plan"),
    pBold("\u7528\u6237\u95EE\u9898\uFF1A\u300C\u4ECA\u5929\u5173\u6CE8\u4EC0\u4E48\uFF1F\u65B9\u5411\u5728\u54EA\uFF1F\u76EE\u6807\u4F4D\u5728\u54EA\uFF1F\u300D"),
    p("\u6838\u5FC3\u903B\u8F91\uFF08\u7F20\u8BBA\u7B2C68\u8BFE\uFF09\uFF1A\u4E0D\u662F\u9884\u6D4B\u4E00\u4E2A\u65B9\u5411\uFF0C\u800C\u662F\u679A\u4E3E\u6240\u6709\u53EF\u80FD\u8D70\u52BF\uFF0C\u6BCF\u79CD\u914D\u4E00\u4E2A\u9884\u6848\uFF1A"),
    codeBlock("f(price) = \u64CD\u4F5CA, if \u4EF7\u683C\u8FDB\u5165\u4E70\u70B9\u533A\u57DF"),
    codeBlock("f(price) = \u64CD\u4F5CB, if \u4EF7\u683C\u8FDB\u5165\u5356\u70B9\u533A\u57DF"),
    codeBlock("f(price) = \u6301\u6709,  if \u4EF7\u683C\u5728\u7ED3\u6784\u5185\u8FD0\u884C"),
    pBold("\u76EE\u6807\u4F4D\u8BA1\u7B97\u56DB\u79CD\u65B9\u6CD5\uFF1A"),
    numbered("\u4E2D\u67A2\u8FB9\u754C\u6CD5\uFF1AZG/ZD \u2192 \u7B2C\u4E00\u7EA7\u652F\u6491/\u963B\u529B"),
    numbered("\u533A\u95F4\u5957\u7CBE\u5EA6\u6CD5\uFF1A\u5927\u7EA7\u522B \u2192 \u6B21\u7EA7\u522B \u2192 \u66F4\u5C0F\u7EA7\u522B\u7CBE\u786E\u533A\u95F4"),
    numbered("\u65B0\u4E2D\u67A2\u5F62\u6210\u76EE\u6807\u6CD5\uFF1A\u524D\u4E00\u4E2D\u67A2ZG \u2192 \u65B0\u4E2D\u67A2\u4F4D\u7F6E\u9884\u4F30"),
    numbered("\u659C\u6CE2\u90A3\u5951\u5EF6\u4F38\u6CD5\uFF1A\u7B14\u7684\u7B49\u957F/0.618/1.618"),
    pageBreak(),

    // ── Monitor ──
    h1("\u56DB\u3001\u76D8\u4E2D\u76D1\u63A7 monitor"),
    pBold("\u7528\u6237\u95EE\u9898\uFF1A\u300C\u73B0\u5728\u6709\u4EC0\u4E48\u4FE1\u53F7\uFF1F\u5E02\u573A\u8D70\u5230\u4E86\u54EA\u4E2A\u5206\u7C7B\uFF1F\u300D"),
    p("\u76D8\u524D\u8BA1\u5212\u5B9A\u4E49\u4E86\u5730\u56FE\uFF08\u5B8C\u5168\u5206\u7C7B\uFF09\uFF0C\u76D8\u4E2D\u5C31\u662F\u8DDF\u8E2A\u5E02\u573A\u8D70\u5230\u4E86\u5730\u56FE\u7684\u54EA\u4E2A\u4F4D\u7F6E\u3002"),
    bullet("\u76D1\u63A7\u64CD\u4F5C\u7EA7\u522BK\u7EBF \u2192 \u7B14\u5B8C\u6210/\u7EBF\u6BB5\u8F6C\u6298/\u80CC\u9A70\u53D1\u5C55"),
    bullet("\u5F53\u4EF7\u683C\u7A7F\u8D8A\u5206\u7C7B\u8FB9\u754C \u2192 \u5206\u7C7B\u72B6\u6001\u5207\u6362 \u2192 \u6267\u884C\u5BF9\u5E94\u9884\u6848"),
    bullet("\u591A\u7EA7\u522B\u4EA4\u53C9\u9A8C\u8BC1\uFF1A\u5927\u7EA7\u522B\u4FE1\u53F7 + \u6B21\u7EA7\u522B\u7CBE\u786E\u5B9A\u4F4D"),
    pBold("\u4E0E\u5F53\u524D intraday \u7684\u5173\u7CFB\uFF1A"),
    bullet("\u5F53\u524D intraday \u5DF2\u5B9E\u73B0 L1+L2+L3 \u626B\u63CF"),
    bullet("\u589E\u5F3A\uFF1A\u5F15\u5165\u300C\u5206\u7C7B\u72B6\u6001\u8DDF\u8E2A\u300D\uFF08\u5173\u8054\u76D8\u524D\u8BA1\u5212\uFF09"),
    bullet("\u4FE1\u53F7\u63A8\u9001\u589E\u52A0\u300C\u5206\u7C7B\u4E0A\u4E0B\u6587\u300D"),
    pageBreak(),

    // ── Review ──
    h1("\u4E94\u3001\u76D8\u540E\u590D\u76D8 review"),
    pBold("\u7528\u6237\u95EE\u9898\uFF1A\u300C\u4ECA\u5929\u53D1\u751F\u4E86\u4EC0\u4E48\uFF1F\u54EA\u91CC\u505A\u9519\u4E86\uFF1F\u300D"),
    p("\u4E0D\u662F\u7B80\u5355\u770B\u4ECA\u5929\u6DA8\u8DCC\uFF0C\u800C\u662F\u5F52\u56E0\u5206\u6790\uFF1A"),
    bullet("\u9010K\u7EBF\u56DE\u653E\u4ECA\u65E5\u4FE1\u53F7\u65F6\u95F4\u7EBF\uFF08\u4F55\u65F6\u51FA\u73B0/\u786E\u8BA4/\u6D88\u5931\uFF09"),
    bullet("\u635F\u5931\u5206\u7C7B\uFF1AA\u7C7B(\u7CFB\u7EDF\u65B9\u5DEE) / B\u7C7B(\u6267\u884C\u5931\u8BEF) / C\u7C7B(\u60C5\u7EEA\u4EA4\u6613)"),
    bullet("\u9057\u6F0F\u5206\u6790\uFF1A\u7CFB\u7EDF\u53D1\u51FA\u4E86\u4FE1\u53F7\u4F46\u672A\u64CD\u4F5C \u2192 \u673A\u4F1A\u6210\u672C\u591A\u5C11\uFF1F"),
    bullet("\u76D8\u524D\u5206\u7C7B vs \u5B9E\u9645\u8D70\u52BF\uFF1A\u9884\u5224\u5BF9\u4E86\u5417\uFF1F"),
    pageBreak(),

    // ── CLI ──
    h1("\u516D\u3001CLI \u8BBE\u8BA1"),
    codeBlock("python run.py plan                  # \u76D8\u524D\u8BA1\u5212"),
    codeBlock("python run.py                       # \u76D8\u4E2D\u76D1\u63A7\uFF08\u9ED8\u8BA4\uFF09"),
    codeBlock("python run.py review                # \u76D8\u540E\u590D\u76D8"),
    codeBlock("python run.py weekly                # \u5468\u672B\u7B56\u7565"),
    codeBlock("python run.py replay --start 2026-03-05   # \u5386\u53F2\u56DE\u653E"),
    codeBlock("python run.py --index               # \u5FEB\u901F\u770B\u6307\u6570"),
    spacer(),
    p("\u5411\u540E\u517C\u5BB9\uFF1A\u65E7 --mode intraday/review/sim \u4ECD\u53EF\u7528\uFF0C\u5185\u90E8\u8DEF\u7531\u5230\u65B0\u6D41\u7A0B\u3002"),
    pageBreak(),

    // ── Roadmap ──
    h1("\u4E03\u3001\u5B9E\u73B0\u8DEF\u7EBF\u56FE"),
    p("\u8FD9\u662F\u4E00\u4E2A\u5927\u578B\u91CD\u6784\uFF0C\u5FC5\u987B\u5206\u6279\u8C28\u614E\u5B9E\u65BD\u3002"),
    h2("Phase 1: \u4FE1\u53F7\u56DE\u653E\u5F15\u64CE\uFF08\u57FA\u5EFA\u5C42\uFF09"),
    p("\u56DE\u653E\u5F15\u64CE\u662F review / replay / plan \u7684\u5171\u540C\u57FA\u7840\u8BBE\u65BD\u3002"),
    new Table({
      width: { size: 9840, type: WidthType.DXA },
      columnWidths: [3500, 1500, 4840],
      rows: [
        new TableRow({ children: [headerCell("\u6587\u4EF6", 3500), headerCell("\u53D8\u66F4", 1500), headerCell("\u8BF4\u660E", 4840)] }),
        new TableRow({ children: [cell("signals/core/replay.py", 3500), boldCell("\u65B0\u5EFA", 1500), cell("SignalReplayer \u9010K\u7EBF\u56DE\u653E\uFF0C\u8BB0\u5F55\u4FE1\u53F7\u53D8\u5316\u65F6\u95F4\u7EBF", 4840)] }),
        new TableRow({ children: [cell("signals/dashboard/__init__.py", 3500), boldCell("\u4FEE\u6539", 1500), cell("\u6DFB\u52A0 replay phase \u8FDB\u5EA6\u663E\u793A", 4840)] }),
      ]
    }),
    spacer(),
    h2("Phase 2: \u76D8\u540E\u590D\u76D8 review"),
    new Table({
      width: { size: 9840, type: WidthType.DXA },
      columnWidths: [3500, 6340],
      rows: [
        new TableRow({ children: [headerCell("\u6587\u4EF6", 3500), headerCell("\u53D8\u66F4", 6340)] }),
        new TableRow({ children: [cell("run.py", 3500), cell("\u65B0\u589E run_review_v2()\uFF0C\u8F93\u51FA\u4FE1\u53F7\u65F6\u95F4\u7EBF + \u7ED3\u6784\u53D8\u5316", 6340)] }),
        new TableRow({ children: [cell("signals/core/replay.py", 3500), cell("\u6DFB\u52A0 print_timeline() \u683C\u5F0F\u5316\u8F93\u51FA", 6340)] }),
      ]
    }),
    spacer(),
    h2("Phase 3: \u76D8\u524D\u8BA1\u5212 plan"),
    new Table({
      width: { size: 9840, type: WidthType.DXA },
      columnWidths: [3500, 6340],
      rows: [
        new TableRow({ children: [headerCell("\u6587\u4EF6", 3500), headerCell("\u53D8\u66F4", 6340)] }),
        new TableRow({ children: [cell("signals/core/planner.py", 3500), cell("\u65B0\u5EFA \u5B8C\u5168\u5206\u7C7B\u751F\u6210\u5668 + \u76EE\u6807\u4F4D\u8BA1\u7B97", 6340)] }),
        new TableRow({ children: [cell("run.py", 3500), cell("\u65B0\u589E run_plan()", 6340)] }),
      ]
    }),
    spacer(),
    h2("Phase 4: \u5468\u672B\u7B56\u7565 weekly"),
    new Table({
      width: { size: 9840, type: WidthType.DXA },
      columnWidths: [3500, 6340],
      rows: [
        new TableRow({ children: [headerCell("\u6587\u4EF6", 3500), headerCell("\u53D8\u66F4", 6340)] }),
        new TableRow({ children: [cell("signals/core/weekly.py", 3500), cell("\u65B0\u5EFA \u5468\u5EA6\u805A\u5408 + \u4E8B\u4EF6\u9884\u6848\u751F\u6210", 6340)] }),
        new TableRow({ children: [cell("run.py", 3500), cell("\u65B0\u589E run_weekly()", 6340)] }),
      ]
    }),
    spacer(),
    h2("Phase 5\uFF08\u8FDC\u671F\uFF09: \u4EA4\u6613\u8BB0\u5F55 + \u5F52\u56E0\u95ED\u73AF"),
    bullet("\u4EA4\u6613\u65E5\u5FD7\u7CFB\u7EDF\uFF08\u624B\u52A8\u5F55\u5165\u6216\u5238\u5546API\u5BFC\u5165\uFF09"),
    bullet("\u64CD\u4F5C\u8BC4\u5206\uFF08\u6267\u884C\u5206 1-5 + A/B/C \u5206\u7C7B\uFF09"),
    bullet("\u9057\u6F0F\u5206\u6790\uFF08\u4FE1\u53F7 vs \u5B9E\u9645\u64CD\u4F5C\uFF09"),
    bullet("\u6708\u5EA6/\u5B63\u5EA6\u7EDF\u8BA1\u4EEA\u8868\u76D8"),
    pageBreak(),

    // ── Current Architecture ──
    h1("\u516B\u3001\u73B0\u6709\u7CFB\u7EDF\u67B6\u6784"),
    codeBlock("signals/"),
    codeBlock("  core/          # \u5206\u6790\u5F15\u64CE"),
    codeBlock("    analyzer.py     # SymbolAnalyzer (CZSC wrapper)"),
    codeBlock("    detectors.py    # detect_all_signals()"),
    codeBlock("    scorer.py       # \u8BC4\u5206\u7CFB\u7EDF"),
    codeBlock("    risk.py         # \u98CE\u9669\u5206\u5C42\u4ED3\u4F4D"),
    codeBlock("    ma_levels.py    # MA\u5747\u7EBF\u5173\u952E\u4F4D"),
    codeBlock("    rotation.py     # \u4E09\u7EBF\u8F6E\u52A8"),
    codeBlock("  data/          # \u591A\u6570\u636E\u6E90"),
    codeBlock("    fetcher.py      # \u7EDF\u4E00\u6570\u636E\u63A5\u53E3"),
    codeBlock("    warehouse.py    # \u6570\u636E\u4ED3\u5E93"),
    codeBlock("    sim_source.py   # \u4EFF\u771F\u6570\u636E\u6E90"),
    codeBlock("  layers/        # \u4E09\u5C42\u8054\u52A8"),
    codeBlock("    index_screener.py  # L1 \u6307\u6570\u7814\u5224"),
    codeBlock("    industry.py        # L2 \u884C\u4E1A\u7814\u5224"),
    codeBlock("    screener.py        # L3 \u6807\u7684\u7B5B\u9009"),
    codeBlock("  dashboard/     # \u8FDB\u5EA6\u9762\u677F"),
  ];

  return new Document({
    styles, numbering,
    sections: [{
      properties: sectionProps,
      headers: {
        default: new Header({ children: [new Paragraph({
          alignment: AlignmentType.RIGHT,
          children: [new TextRun({ text: "\u9686\u5C0F\u4FA0 \u67B6\u6784\u91CD\u6784\u65B9\u6848", font: FONT, size: 18, color: "999999" })]
        })] })
      },
      footers: {
        default: new Footer({ children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [new TextRun({ text: "\u7B2C ", font: FONT, size: 18, color: "999999" }), new TextRun({ children: [PageNumber.CURRENT], font: FONT, size: 18, color: "999999" }), new TextRun({ text: " \u9875", font: FONT, size: 18, color: "999999" })]
        })] })
      },
      children
    }]
  });
}

// ── Build both documents ──
async function main() {
  const docsDir = "/Users/zhangqilong/Desktop/Signals/docs";

  const researchDoc = buildResearchDoc();
  const researchBuf = await Packer.toBuffer(researchDoc);
  fs.writeFileSync(`${docsDir}/research-findings.docx`, researchBuf);
  console.log(`[OK] research-findings.docx (${(researchBuf.length / 1024).toFixed(0)} KB)`);

  const archDoc = buildArchDoc();
  const archBuf = await Packer.toBuffer(archDoc);
  fs.writeFileSync(`${docsDir}/architecture-plan.docx`, archBuf);
  console.log(`[OK] architecture-plan.docx (${(archBuf.length / 1024).toFixed(0)} KB)`);
}

main().catch(err => { console.error(err); process.exit(1); });
