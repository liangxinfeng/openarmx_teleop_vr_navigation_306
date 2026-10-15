// Exercise the actual frontend chord block without a browser or robot.
const fs = require('fs');
const assert = require('assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('    if (yPressed && !navUi.previous.y)');
const end = source.indexOf('    if (navUi.exitConfirm)', start);
assert(start > 0 && end > start);
const run = new Function('navUi','yPressed','bPressed','xPressed','aPressed',
  'performance','navShowNotice','setBaseControlLatched','sendBaseDrive',
  source.slice(start,end) + '\nreturn "other-controls";');
const state={previous:{y:false,b:false,a:false,x:false},baseControlLatched:true};
let notices=0,stops=0;
function tick(y,b){
  return run(state,y,b,false,false,{now:()=>100},()=>notices++,
    active=>{state.baseControlLatched=active;},active=>{assert(!active);stops++;});
}
assert.equal(tick(true,true),undefined);
assert.equal(state.baseControlLatched,false);
assert.equal(tick(false,true),undefined); // B stays down: no head toggle / skip
assert.equal(tick(false,false),undefined); // final release still consumed
assert.equal(state.previous.b,false);
assert.equal(tick(false,false),'other-controls');
assert.equal(notices,1);assert.equal(stops,3);
assert.equal(tick(true,true),undefined);
assert.equal(tick(true,false),undefined); // Y stays down: no camera / height
assert.equal(tick(false,false),undefined);
assert.equal(state.previous.y,false);
console.log('Y+B staggered-release ownership: passed (both release orders)');
