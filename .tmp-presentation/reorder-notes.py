from pathlib import Path
import re
root=Path('HR-Agent/presentation')
p=root/'speaker-notes.md'
text=p.read_text(encoding='utf-8')
parts=re.split(r'^## Slide (\d+)\s*\n',text,flags=re.M)
sections={int(parts[i]):parts[i+1].strip() for i in range(1,len(parts),2)}
def references(t):
    for old,new in [('slides 8–14','slides 7–13'),('slides 19–23','slides 17–21'),('slide 13','slide 12'),('slide 15','slide 14'),('slide 7','slide 22')]:
        t=t.replace(old,new)
    t=re.sub(r' ?Deployed PostgreSQL verification remains pending\.', '',t)
    t=re.sub(r' ?Deployed PostgreSQL verification pending\.', '',t)
    t=t.replace('; deployed verification pending','')
    return t
test_notes='''APPENDIX / TEST EVIDENCE

The latest recorded non-live suite reports 290 tests passed and 31 database tests skipped. Four separate live-provider checks also passed using local policies and synthetic memory storage. Those checks cover carry-over and medical-channel questions in Indonesian and English, with citations and no action writes.

These are separate verification scopes. The skipped database tests are not passing results. The four live-provider checks are separate from the non-live suite. This slide summarizes saved results, not a new test run.

Source: HR-Agent/submission-evidence/qa-rows-3-4/README.md; HR-Agent/submission-evidence/qa-rows-3-4/responses.json.'''
# Existing Markdown omitted the test-evidence section, so its QA sections were one behind the deck.
ordered=[sections[i] for i in range(1,7)]+[sections[i] for i in range(8,17)]+[test_notes]+[sections[i] for i in range(18,23)]+[sections[7]]
ordered=[references(t) for t in ordered]
ordered[-1]=re.sub(r'The repository records local Docker verification.*?I am not claiming a verified public deployment\.', '',ordered[-1])
intro='''# English speaker notes

22 slides. The 12-minute core talk uses slides 1–6, then jumps to slide 22 for limitations and closing. Slides 7–13 are recorded demo backups, slide 14 is monitoring evidence, and slides 15–16 cover API boundaries and test evidence. Slides 17–21 contain the 15 HR QA prompts. Use optional evidence and QA slides as needed, then return to slide 22 to close. QA02–03 passed four live-provider checks with synthetic memory storage. QA10–15 passed backend checks in both languages. QA01 and QA04–09 remain unverified in this update.
'''
p.write_text(intro+'\n'+ '\n\n'.join(f'## Slide {i}\n\n{body}' for i,body in enumerate(ordered,1))+'\n',encoding='utf-8')
p=root/'rehearsal-guide.md'
t=p.read_text(encoding='utf-8')
start=t.index('## Files and slide navigation')
end=t.index('The backup screenshots',start)
t=t[:start]+'''## Files and slide navigation

- `hr-assistant-presentation-qa-final.pptx`: current editable deck with 22 slides and synchronized speaker notes.
- `speaker-notes.md`: narration arranged in the exact deck order.
- Slides 1–6: core presentation. For a 12-minute talk, jump to slide 22 after slide 6.
- Slides 7–13: recorded demo backups. Return to slide 4 afterward.
- Slide 14: monitoring evidence. Show during slide 6, then return to slide 6 or jump to slide 22.
- Slide 15: Q&A on API boundaries.
- Slide 16: latest recorded test evidence, without dates or deployment status.
- Slides 17–21: all 15 HR QA prompts and their verification scope.
- Slide 22: limitations and next steps, followed by closing. Return here after optional Q&A.

'''+t[end:]
# Update old references in the remainder, keeping the new navigation intact.
head,tail=t.split('The backup screenshots',1)
tail=references(tail)
tail=tail.replace('| 7 | 11:00 | 12:00 |','| 22 | 11:00 | 12:00 |')
tail=re.sub(r'Appendix slide: (\d+)',lambda m:f'Appendix slide: {int(m[1])-2}',tail)
tail=re.sub(r'\*\*Is it publicly deployed\?\*\*[^\n]*\n\n','',tail)
a=tail.index('## Evidence provenance')
b=tail.index('## Complete HR QA prompt set',a)
tail=tail[:a]+'''## Evidence provenance

Slide 16 shows the latest recorded non-live suite: **290 passed, 31 database tests skipped**. Four separate live-provider checks passed with local policies and synthetic memory storage. Source: `../submission-evidence/qa-rows-3-4/README.md` and `responses.json`. Present these as separate scopes, not a combined test total or a new run.

Slide 6 retains the separate 15-scenario evaluation from `../submission-evidence/step11/evaluation_report.json`. Recorded screenshots also come from that evidence directory. These evaluation metrics and demo recordings are separate from the latest suite results.

QA02–03 passed live-provider checks in Indonesian and English. QA10–15 passed backend checks in both languages. QA01 and QA04–09 remain unverified in this update.

'''+tail[b:]
p.write_text(head+'The backup screenshots'+tail,encoding='utf-8')
