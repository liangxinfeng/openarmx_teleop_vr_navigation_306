// Execute the production hold-A block without a browser or robot.
const fs=require('fs'),assert=require('assert/strict');
const text=fs.readFileSync(process.argv[2],'utf8');
const start=text.indexOf("    const taskHoldMode = menuOwnsA ?");
const end=text.indexOf("    if (['navigating', 'navigation_failed'].includes(taskState))",start);
assert(start>0&&end>start);
const run=new Function('taskState','navUi','aPressed','xPressed','menuOwnsA','performance',
  'navShowNotice','navigationAction','let taskAConsumed=false;'+text.slice(start,end));
globalThis.autolifeVrClientId=()=> 'headset';
function scenario(state='failed'){
 const ui={previous:{a:false},state:{task_dispatch:{task_token:'mission:1'}},baseControlLatched:false};
 const calls=[];
 return {ui,calls,state,tick(ms,a=false,x=false,menu=false){
   run(this.state,ui,a,x,menu,{now:()=>ms},()=>{},(...args)=>calls.push(args));ui.previous.a=a;
 }};
}
let s=scenario();s.tick(1,true);s.tick(1100,true);assert(!s.calls.length);s.tick(1200);
assert.deepEqual(s.calls,[['/api/tasks/retry-pose',{task_token:'mission:1',client_id:'headset'}]]);
s.tick(1300);assert.equal(s.calls.length,1);
s=scenario();s.tick(1,true);s.tick(400);assert(!s.calls.length);
for(const change of ['x','menu','drive','token','state']){
 s=scenario();s.tick(1,true);s.tick(1100,true);
 if(change==='drive')s.ui.baseControlLatched=true;
 if(change==='token')s.ui.state.task_dispatch.task_token='mission:2';
 if(change==='state')s.state='awaiting_action';
 s.tick(1200,false,change==='x',change==='menu');assert(!s.calls.length,change);
}
s=scenario();s.tick(1,true,true);s.tick(1100,true);s.tick(1200);assert(!s.calls.length);
for(const [state,path] of [['awaiting_action','complete'],['navigation_failed','retry-navigation-failure']]){
 s=scenario(state);s.tick(1,true);s.tick(1100,true);s.tick(1200);
 assert.equal(s.calls[0][0],'/api/tasks/'+path);
}
console.log('PASS: retry pose on long-A release only, task/owner token, chords, menu, base control, state changes, existing actions');
