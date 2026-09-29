const fs=require('fs'),assert=require('assert/strict');
const text=fs.readFileSync(process.argv[2],'utf8');
const start=text.indexOf('function deliveryDoorPayload()');
const end=text.indexOf('\n// Delivery acceptance',start);
assert(start>0&&end>start);
const make=new Function('navUi','deliveryHandoff','deliveryOfferPending','navShowNotice','navigationAction','drawNavigationPanel',
 text.slice(start,end)+'; return handleDeliveryArrivalInput;');
globalThis.autolifeVrClientId=()=> 'headset';
function scenario(){
 const ui={sessionActive:true,deliveryCardActive:true,menuVisible:true,previous:{},state:{task_dispatch:{state:'awaiting_action'}}};
 const h={state:'ready_to_open',can_open:true,order_id:'test',task_id:'test-pickup',door_id:'1'},calls=[];
 const run=make(ui,()=>h,()=>false,()=>{},(...args)=>{calls.push(args);return Promise.resolve(true);},()=>{});
 return {ui,h,calls,tick(ms,a=false,x=false,y=false,b=false){const used=run(a,x,y,b,ms);ui.previous={a,x,y,b};return used;}};
}
(async()=>{
 let s=scenario();s.tick(0);assert(s.tick(1,true));s.tick(201);assert.equal(s.calls.length,1);
 assert(s.calls[0][0].endsWith('/open'));assert.equal(s.calls[0][1].door_id,'1');
 await Promise.resolve();assert.equal(s.ui.deliveryCardActive,false);assert(s.tick(202));assert(!s.tick(203));
 assert(s.ui.taskHoldCancelled && s.ui.taskRejectCancelled);
 s=scenario();s.tick(0);s.tick(1,true);s.tick(1200,true);s.tick(1201);assert.equal(s.calls.length,0);
 assert(s.ui.deliveryCardActive); // long A neither opens nor completes mission
 s=scenario();s.tick(0,true);s.tick(1500,true);s.tick(1501);assert.equal(s.calls.length,0);
 s.tick(1600,true);s.tick(1800);assert.equal(s.calls.length,1);
 s=scenario();s.tick(0);s.tick(1,false,true);s.tick(1200,false,true);assert(s.tick(1300));
 assert(!s.ui.deliveryCardActive && !s.calls.length);assert(!s.tick(1400));
 s=scenario();s.tick(0);s.tick(1,true);s.h.door_id='2';s.tick(201);assert.equal(s.calls.length,0);
 s=scenario();s.tick(0);s.tick(1,true,true);s.tick(20,true);s.tick(200);assert.equal(s.calls.length,0);
 s=scenario();s.ui.baseControlLatched=true;assert(!s.tick(0,true));assert(!s.calls.length);
 s=scenario();s.h.state='arrived_wait_door';s.h.can_open=false;s.tick(0);s.tick(1,true);s.tick(200);assert(!s.calls.length);
 const hook=text.indexOf('if (handleDeliveryArrivalInput(aPressed');
 assert(hook>0 && hook<text.indexOf('const taskState =',hook));
 const begin=text.indexOf('  const templates = Array.isArray(navUi.state?.task_templates)');
 const stop=text.indexOf('  if (navUi.mapConfirmPath)',begin);
 const menu=new Function('navUi','deliveryHandoff',text.slice(begin,stop));
 const items=menu({templateMode:true,state:{task_templates:[{name:'洗衣房洗衣',id:'wash',task_count:5},{name:'洗衣房取衣',id:'collect',task_count:4}]}},
 ()=>({state:'washing',return_task_chain:'洗衣房取衣'}));
 assert(items.some(i=>i.kind==='template-candidate'&&i.name==='洗衣房取衣'));
 assert(items.some(i=>i.kind==='delivery-return')); // separate explicit dispatch action
 console.log('PASS: short A open vs long A task isolation, X dismiss, held keys, door changes, chords, manual control, all TXT templates');
})().catch(e=>{console.error(e);process.exitCode=1;});
