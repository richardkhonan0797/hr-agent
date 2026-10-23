import fs from 'node:fs/promises';
import { PresentationFile, FileBlob } from 'file:///C:/Users/ricat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@oai/artifact-tool/dist/artifact_tool.mjs';
const p = await PresentationFile.importPptx(await FileBlob.load('HR-Agent/presentation/hr-assistant-presentation-qa-improvements.pptx'));
console.log(await p.inspect({kind:'slide,textbox,notes', search:'test evidence', maxChars:12000}));
const s=p.slides.items[16];
console.log(s.shapes.items.map(x=>({id:x.id,text:String(x.text),position:x.position})));
await fs.writeFile('.tmp-presentation/test-before.png',new Uint8Array(await (await s.export({format:'png',scale:1})).arrayBuffer()));


