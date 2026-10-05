/**
 * Generates a synthetic but structurally realistic P&ID, as a vector PDF with
 * a real text layer and real stroked geometry — the same shape of input a CAD
 * export produces.
 *
 * Exists so the pipeline can be exercised end to end without a customer
 * drawing and without guessing at ground truth: what is on this sheet is
 * exactly what this file draws, so extraction recall can actually be measured.
 *
 *   node test/fixtures/make-pid.mjs /tmp/pid-sample.pdf
 *
 * Ground truth: 4 tagged equipment (T-101, P-101A, P-101B, E-201),
 * 6 instruments (LT-101, LIC-101, FT-202, FIC-202, TT-204, TIC-204),
 * 4 valves (PSV-201, HV-201, HV-215, FCV-202), 6 process lines
 * (12"-CR-2201-A1, 10"-CR-2202-A1, 8"-CR-2203-A1, 8"-CR-2204-B1,
 * 4"-RV-2210-A1, 2"-DR-2215-A1), 4 notes, 1 off-page connector.
 */
import { writeFileSync } from 'node:fs'
const W = 1190, H = 842
const ops = []
const P = (n) => (+n).toFixed(2)
const move = (x,y)=>ops.push(`${P(x)} ${P(y)} m`)
const line = (x,y)=>ops.push(`${P(x)} ${P(y)} l`)
const stroke = (w=1)=>ops.push(`${P(w)} w S`)
const rect = (x,y,w,h,lw=1)=>ops.push(`${P(lw)} w ${P(x)} ${P(y)} ${P(w)} ${P(h)} re S`)
const K = 0.5523
function circle(cx,cy,r,lw=1){
  ops.push(`${P(lw)} w`)
  move(cx+r,cy)
  ops.push(`${P(cx+r)} ${P(cy+r*K)} ${P(cx+r*K)} ${P(cy+r)} ${P(cx)} ${P(cy+r)} c`)
  ops.push(`${P(cx-r*K)} ${P(cy+r)} ${P(cx-r)} ${P(cy+r*K)} ${P(cx-r)} ${P(cy)} c`)
  ops.push(`${P(cx-r)} ${P(cy-r*K)} ${P(cx-r*K)} ${P(cy-r)} ${P(cx)} ${P(cy-r)} c`)
  ops.push(`${P(cx+r*K)} ${P(cy-r)} ${P(cx+r)} ${P(cy-r*K)} ${P(cx+r)} ${P(cy)} c`)
  ops.push('S')
}
const texts = []
function text(s,x,y,size=8){ texts.push({s,x,y,size}); }
function polyline(pts,lw=1.6){ move(pts[0][0],pts[0][1]); for(const p of pts.slice(1)) line(p[0],p[1]); stroke(lw) }
// bowtie valve
function valve(cx,cy,r=7){
  move(cx-r,cy-r); line(cx-r,cy+r); line(cx+r,cy-r); line(cx+r,cy+r); line(cx-r,cy-r); stroke(1.2)
}
function instrument(cx,cy,tag,loop,panel=false){
  circle(cx,cy,14,1)
  if(panel){ move(cx-14,cy); line(cx+14,cy); stroke(0.8) }
  text(tag,cx-11,cy+3,7); text(loop,cx-9,cy-8,7)
}

// border + title block
rect(20,20,W-40,H-40,1.5)
rect(W-320,25,295,110,1)
ops.push('1 w')
move(W-320,95); line(W-25,95); stroke(0.8)
move(W-320,65); line(W-25,65); stroke(0.8)
text('PIPING & INSTRUMENTATION DIAGRAM',W-312,112,10)
text('CRUDE FEED SURGE & CHARGE PUMPS',W-312,100,8)
text('DWG No.  PID-2201-001',W-312,80,8)
text('REV  3',W-150,80,8)
text('SHEET 1 OF 4',W-312,70,7)
text('UNIT 2200 - CRUDE DISTILLATION',W-312,52,7)
text('CLIENT: NORTHPORT REFINING',W-312,42,7)
text('SCALE: NTS',W-150,42,7)
text('DATE: 2026-03-14',W-150,52,7)

// T-101 surge drum (left)
rect(90,520,150,170,1.4)
text('T-101',140,700,10)
text('CRUDE SURGE DRUM',95,505,7)
text('DESIGN 10.5 barg / 150 C',95,495,6)
text('CAP. 45 m3',95,485,6)

// level instruments on drum
instrument(300,660,'LT','101')
instrument(300,600,'LIC','101',true)
move(240,660); line(286,660); stroke(0.7)
move(300,646); line(300,614); stroke(0.7)
ops.push('[3 2] 0 d'); move(314,600); line(420,600); stroke(0.7); ops.push('[] 0 d')

// feed line into drum
polyline([[40,660],[90,660]],2)
text('12"-CR-2201-A1',40,668,7)
text('FROM TK-050',40,648,6)

// drum -> pump suction header
polyline([[165,520],[165,420],[430,420]],2)
text('10"-CR-2202-A1',200,428,7)
valve(260,420); text('HV-201',248,400,7)

// P-101A / P-101B pumps
circle(470,455,26,1.4); text('P-101A',452,452,8)
circle(470,360,26,1.4); text('P-101B',452,357,8)
polyline([[430,420],[430,455],[444,455]],2)
polyline([[430,420],[430,360],[444,360]],2)
text('CHARGE PUMPS 2x100%',430,318,6)
text('75 kW / 3560 rpm',430,308,6)

// pump discharge -> header
polyline([[496,455],[560,455],[560,410]],2)
polyline([[496,360],[560,360],[560,410]],2)
polyline([[560,410],[700,410]],2)
text('8"-CR-2203-A1',600,418,7)
valve(620,410); text('FCV-202',606,390,7)
instrument(620,470,'FIC','202',true)
ops.push('[3 2] 0 d'); move(620,456); line(620,424); stroke(0.7); ops.push('[] 0 d')
instrument(540,470,'FT','202')
move(540,456); line(540,412); stroke(0.7)

// E-201 exchanger
rect(700,350,170,120,1.4)
circle(785,410,52,1.2)
text('E-201',770,500,10)
text('CRUDE PREHEATER',700,335,7)
text('DUTY 4.2 MW',700,325,6)
text('SHELL 12 barg / 260 C',700,315,6)

// exchanger outlet
polyline([[870,410],[1000,410]],2)
text('8"-CR-2204-B1',900,418,7)
instrument(950,470,'TT','204')
move(950,456); line(950,412); stroke(0.7)
instrument(1010,470,'TIC','204',true)

// off page connector
polyline([[1000,410],[1030,410],[1040,418],[1050,410],[1040,402],[1030,410]],1.5)
text('TO PID-2201-002',1000,388,7)
text('ATMOS. TOWER C-201',1000,378,6)

// relief line
polyline([[200,690],[200,760],[380,760]],1.6)
text('4"-RV-2210-A1',210,768,7)
circle(240,725,12,1.2); text('PSV',232,722,6); text('201',233,712,6)
text('SET 10.5 barg',255,722,6)
text('TO FLARE HEADER',390,757,7)

// drain
polyline([[140,520],[140,470],[210,470]],1.4)
text('2"-DR-2215-A1',145,455,7)
valve(180,470); text('HV-215',168,450,7)

// notes
text('NOTES:',60,250,9)
text('1. ALL LINES TO BE HYDROTESTED PER SPEC PS-1100.',60,236,7)
text('2. PUMP P-101B IS INSTALLED SPARE, AUTO-START ON LOW DISCHARGE PRESSURE.',60,224,7)
text('3. PSV-201 DISCHARGES TO THE UNIT 2200 FLARE HEADER.',60,212,7)
text('4. INSULATION: H = HOT SERVICE, P = PERSONNEL PROTECTION.',60,200,7)

// legend
text('LEGEND',600,250,9)
text('---  PROCESS LINE',600,236,7)
text('- -  ELECTRICAL SIGNAL',600,224,7)
text('(O)  FIELD INSTRUMENT',600,212,7)
text('(-)  PANEL MOUNTED INSTRUMENT',600,200,7)

const textOps = texts.map(t=>`BT /F1 ${t.size} Tf ${P(t.x)} ${P(t.y)} Td (${t.s.replace(/[()\\]/g,'\\$&')}) Tj ET`).join('\n')
const content = ops.join('\n') + '\n' + textOps

const objs=[]
objs[1]='<< /Type /Catalog /Pages 2 0 R >>'
objs[2]='<< /Type /Pages /Kids [3 0 R] /Count 1 >>'
objs[3]=`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${W} ${H}] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>`
objs[4]=`<< /Length ${content.length} >>\nstream\n${content}\nendstream`
objs[5]='<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'
let pdf='%PDF-1.4\n'; const off=[]
for(let i=1;i<=5;i++){off[i]=pdf.length; pdf+=`${i} 0 obj\n${objs[i]}\nendobj\n`}
const xref=pdf.length
pdf+=`xref\n0 6\n0000000000 65535 f \n`
for(let i=1;i<=5;i++) pdf+=`${String(off[i]).padStart(10,'0')} 00000 n \n`
pdf+=`trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`
writeFileSync(process.argv[2],pdf,'latin1')
console.log('wrote',process.argv[2],'—',texts.length,'text items')
