// Actual production gesture block; no browser or robot connections.
const fs=require('fs'), assert=require('assert/strict');
const text=fs.readFileSync(process.argv[2],'utf8');
const begin=text.indexOf("    if (['navigating', 'navigation_failed'].includes(taskState))");
const end=text.indexOf('    if (!navUi.baseControlLatched && !yPressed',begin);
assert(begin>0&&end>begin);
const run=new Function('taskState','navUi','bPressed','aPressed','xPressed','yPressed',
 'performance','navShowNotice','navigationAction','toggleHeadMotionLock','setBaseControlLatched',
 text.slice(begin,end));
globalThis.autolifeVrClientId=()=> 'headset';
function scenario(state='navigating'){
 const ui={previous:{b:false},state:{task_dispatch:{task_token:'mission:0'}}};
 const calls=[];let heads=0,stops=0;
 return {ui,calls,get heads(){return heads},get stops(){return stops},
 tick(ms,b,a=false,x=false,y=false){run(state,ui,b,a,x,y,{now:()=>ms},()=>{},
 (url,p)=>calls.push([url,p]),()=>heads++,()=>stops++);ui.previous.b=b;}};
}
for(const state of ['navigating','navigation_failed']){
 const s=scenario(state);s.tick(1,true);s.tick(1050,true);assert.equal(s.calls.length,0);
 s.tick(1100,false);assert.equal(s.calls.length,1);assert.equal(s.calls[0][0],'/api/tasks/operator-arrival');
 assert.equal(s.calls[0][1].task_token,'mission:0');assert.equal(s.heads,0);assert.equal(s.stops,1);
 s.tick(1200,false);assert.equal(s.calls.length,1);
}
const short=scenario();short.tick(1,true);short.tick(250,false);assert.equal(short.heads,1);assert.equal(short.calls.length,0);
for(const chord of ['a','x','y']){
 const s=scenario();s.tick(1,true);s.tick(1100,true,chord==='a',chord==='x',chord==='y');
 s.tick(1200,false);assert.equal(s.calls.length,0);
}
const stale=scenario();stale.tick(1,true);stale.tick(1050,true);stale.ui.state.task_dispatch.task_token='mission:1';
stale.tick(1100,false);assert.equal(stale.calls.length,0);
const arrived=scenario('awaiting_action');arrived.tick(1,true);arrived.tick(1050,true);arrived.tick(1100,false);
assert.equal(arrived.calls.length,0);
assert(!text.includes("navigationAction('/api/tasks/skip-navigation-failure')"));
assert(text.includes("confirmed: true, task_token: pending?.token"));
assert(text.includes('setBaseControlLatched(!navUi.baseControlLatched, true, navUi.baseControlLatched)'));
console.log('PASS: B arrival not skip, release-once, short B, chords, stale task, explicit base release');
