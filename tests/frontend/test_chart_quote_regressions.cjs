const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(require('path').resolve(__dirname,'../../frontend/chart_workspace.js'),'utf8');
const code=source.slice(source.indexOf('    function applyOrderTicketPrice('),source.indexOf('    /* ---- Chart status bar'));
const pending=[],state={symbol:'AAPL',tvLastPrice:null};
const ctx={state,renderOrderPreview:()=>{},AbortSignal,fetch:url=>new Promise(resolve=>pending.push({url,resolve}))};
vm.createContext(ctx);vm.runInContext(code,ctx);
const reply=(i,price,cache='fresh',time=Date.now()/1000)=>pending[i].resolve({ok:true,json:async()=>({cache,bars:[{time,close:price}]})});
(async()=>{
 const a=ctx.fetchLivePrice('AAPL');state.symbol='NVDA';const b=ctx.fetchLivePrice('NVDA');
 reply(1,200);await b;reply(0,100);await a;assert.equal(state.tvLastPrice,200);
 ctx.applyOrderTicketPrice({cache:'fresh',bars:[{time:1577836800,close:123}]},'NVDA',state.priceRequestId);assert.equal(state.tvLastPrice,null);
 state.tvLastPrice=123;const c=ctx.fetchLivePrice('NVDA');pending[2].resolve({ok:false});await c;assert.equal(state.tvLastPrice,null);
 const d=ctx.fetchLivePrice('NVDA');reply(3,300,'stale');await new Promise(setImmediate);
 assert.equal(state.tvLastPrice,null);state.symbol='AAPL';const e=ctx.fetchLivePrice('AAPL');
 reply(5,150);await e;reply(4,310);await d;assert.equal(state.tvLastPrice,150);
 const result={symbolRace:'pass',oldBarRejected:'pass',errorInvalidates:'pass',staleRefreshRace:'pass'};
 console.log(result);
})();
