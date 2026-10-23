import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import {PresentationFile,FileBlob} from 'file:///C:/Users/ricat/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@oai/artifact-tool/dist/artifact_tool.mjs';
const source=await PresentationFile.importPptx(await FileBlob.load('HR-Agent/presentation/hr-assistant-presentation-qa-reasons.pptx'));
const final=await PresentationFile.importPptx(await FileBlob.load('HR-Agent/presentation/hr-assistant-presentation-qa-improvements.pptx'));
const changed=[];
for(let i=0;i<final.slides.items.length;i++){
 const a=Buffer.from(await (await source.slides.items[i].export({format:'png',scale:1})).arrayBuffer());
 const b=Buffer.from(await (await final.slides.items[i].export({format:'png',scale:1})).arrayBuffer());
 await fs.writeFile(`.tmp-presentation/final-${i+1}.png`,b);
 if(!a.equals(b))changed.push(i+1);
}
console.log(JSON.stringify({slides:final.slides.items.length,visuallyChangedSlides:changed,notesContainImprovement:final.slides.items[6].speakerNotes.text.includes('only for demo purposes')}));
