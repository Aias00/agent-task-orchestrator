// One authorized HTTPS request per invocation. No SDK retry, redirects, shell or Web tool.
import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import tls from 'node:tls';
import https from 'node:https';
import {once} from 'node:events';
import {StringDecoder} from 'node:string_decoder';

export async function request(config, body, key, grant, {ca, signal, onResponse}={}) {
  const url=new URL(config.endpoint);
  if(url.protocol!=='https:'||url.username||url.password||url.search||url.hash||url.pathname!=='/v1') throw Error('Production endpoint rejected');
  if(typeof key!=='string'||!key||/[\r\n]/.test(key)||!Number.isInteger(grant.remaining_ms)||grant.remaining_ms<1) throw Error('Production credential or deadline rejected');
  let socket,secure,agent,req;
  const deadline=new AbortController();
  signal=signal?AbortSignal.any([signal,deadline.signal]):deadline.signal;
  const abort=()=>{req?.destroy();secure?.destroy();socket?.destroy();};
  const timer=setTimeout(()=>deadline.abort(),Math.min(grant.remaining_ms,60000));
  signal?.addEventListener('abort',abort,{once:true});
  try {
    if(signal?.aborted) throw Error('Production cancelled');
    socket=net.connect({host:'127.0.0.1',port:config.proxy_port});
    socket.setTimeout(Math.min(2000,grant.remaining_ms),()=>deadline.abort());
    await once(socket,'connect',{signal});
    socket.write(`CONNECT ${url.hostname}:443 HTTP/1.1\r\nHost: ${url.hostname}:443\r\nProxy-Authorization: Bearer ${grant.egress_grant}\r\n\r\n`);
    let head='';
    while(!head.includes('\r\n\r\n')) {
      const [chunk]=await once(socket,'data',{signal});head+=chunk.toString('ascii');
      if(head.length>4096) throw Error('Production CONNECT limit');
    }
    if(head!=='HTTP/1.1 200 Connection Established\r\n\r\n') throw Error('Production CONNECT rejected');
    secure=tls.connect({socket,servername:url.hostname,rejectUnauthorized:true,...(ca?{ca}:{})});
    await once(secure,'secureConnect',{signal});
    agent=new https.Agent({keepAlive:false,maxSockets:1});
    // HTTPS never gets a default socket-creation path or an ambient proxy.
    agent.createConnection=()=>secure;
    const response=await new Promise((resolve,reject)=>{
      req=https.request({hostname:url.hostname,port:443,path:'/v1/chat/completions',method:'POST',agent,
        headers:{'Content-Type':'application/json',Authorization:`Bearer ${key}`,'Content-Length':Buffer.byteLength(body)},signal},resolve);
      req.on('error',reject);req.end(body);
    });
    await onResponse?.(new Response(null,{status:response.statusCode,headers:response.headers}));
    if(response.statusCode!==200) {response.destroy();throw Error('Production upstream rejected');}
    let bytes=0,pending='',events=[];const decoder=new StringDecoder('utf8');
    for await(const chunk of response) {
      bytes+=chunk.length;if(bytes>2*1024*1024) throw Error('Production response limit');
      pending+=decoder.write(chunk);
      let boundary;
      while((boundary=/\r?\n\r?\n/.exec(pending))) {
        const frame=pending.slice(0,boundary.index);pending=pending.slice(boundary.index+boundary[0].length);
        if(frame.length>1024*1024) throw Error('Production frame limit');
        const data=frame.split(/\r?\n/).filter(x=>x.startsWith('data:')).map(x=>x.slice(5).trimStart()).join('\n');
        if(data) events.push(data);
      }
      if(pending.length>1024*1024) throw Error('Production frame limit');
    }
    if(pending.trim()||!events.includes('[DONE]')) throw Error('Production incomplete stream');
    return events;
  } finally {
    clearTimeout(timer);signal?.removeEventListener('abort',abort);abort();agent?.destroy();
  }
}

export function payload(config, context) {
  const messages=[],tools=[];
  for(const m of context.messages) {
    if(m.role==='system') {
      messages.push({role:'system',content:m.content.filter(x=>x.type==='text').map(x=>x.text).join('\n')});
      for(const t of m.tools??[]) tools.push({type:'function',function:{name:t.name,description:t.description,parameters:t.parameters}});
    } else if(m.role==='user') {
      if(m.content.some(x=>x.type!=='text')) throw Error('Production text input only');
      messages.push({role:'user',content:m.content.map(x=>x.text).join('\n')});
    } else if(m.role==='assistant') {
      const calls=m.content.filter(x=>x.type==='toolCall').map(x=>({id:x.id,type:'function',function:{name:x.name,arguments:JSON.stringify(x.arguments)}}));
      messages.push({role:'assistant',content:m.content.filter(x=>x.type==='text').map(x=>x.text).join(''),...(calls.length?{tool_calls:calls}:{})});
    } else if(m.role==='toolResult') {
      if(m.content.some(x=>x.type!=='text')) throw Error('Production text tool output only');
      messages.push({role:'tool',tool_call_id:m.toolCallId,content:m.content.map(x=>x.text).join('\n')});
    } else throw Error('Production message type unsupported');
  }
  if(tools.some(x=>!['read','edit','write'].includes(x.function.name))) throw Error('Production tool rejected');
  return {model:config.model_id,messages,stream:true,max_tokens:config.output_tokens,...(tools.length?{tools}:{})};
}

export function decode(events, model, secret) {
  const content=[],calls=new Map();let text='',finish,done=false,usage;
  for(const raw of events) {
    if(raw==='[DONE]'){done=true;continue;}
    if(done) throw Error('Production data after terminal');
    const event=JSON.parse(raw);
    if(secret&&JSON.stringify(event).includes(secret)) throw Error('Production secret reflection rejected');
    if(event.error) throw Error('Production error event');
    if(event.usage) usage=event.usage;
    for(const choice of event.choices??[]) {
      if(choice.index!==0) throw Error('Production multiple choices unsupported');
      const delta=choice.delta??{};
      if(delta.content!=null) {if(typeof delta.content!=='string') throw Error('Production delta rejected');text+=delta.content;}
      for(const part of delta.tool_calls??[]) {
        if(!Number.isInteger(part.index)||part.index<0||part.index>15) throw Error('Production tool index');
        const call=calls.get(part.index)??{type:'toolCall',id:'',name:'',raw:''};
        if(part.id)call.id+=part.id;
        if(part.function?.name)call.name+=part.function.name;
        if(part.function?.arguments)call.raw+=part.function.arguments;
        if(call.raw.length>65536) throw Error('Production tool argument limit');calls.set(part.index,call);
      }
      if(choice.finish_reason)finish=choice.finish_reason;
    }
    if(text.length>1024*1024) throw Error('Production answer limit');
  }
  if(!done||!['stop','tool_calls'].includes(finish)) throw Error('Production terminal rejected');
  if(text)content.push({type:'text',text});
  for(const call of calls.values()) {
    if(!call.id||!['read','edit','write'].includes(call.name)) throw Error('Production tool rejected');
    content.push({type:'toolCall',id:call.id,name:call.name,arguments:JSON.parse(call.raw)});
  }
  const input=usage?.prompt_tokens??0,output=usage?.completion_tokens??0;
  if(!Number.isInteger(input)||input<0||!Number.isInteger(output)||output<0) throw Error('Production usage rejected');
  return {role:'assistant',api:model.api,provider:model.provider,model:model.id,content,
    stopReason:finish==='tool_calls'?'toolUse':'stop',timestamp:Date.now(),usage:{input,output,
    cacheRead:0,cacheWrite:0,totalTokens:input+output,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
}

class Stream {
  constructor(){this.ready=new Promise(resolve=>{this.finish=resolve;});}
  async *[Symbol.asyncIterator](){await this.ready;const m=this.message;
    yield m.stopReason==='error'?{type:'error',reason:'error',error:m}:{type:'done',reason:m.stopReason,message:m};}
  async result(){await this.ready;return this.message;}
}

export default function(pi) {
  const config=JSON.parse(fs.readFileSync(path.join(process.env.CTASK_PI_RUNTIME,'trusted/production.json'),'utf8'));
  if(config.profile_hash!==process.env.ATO_PI_PROFILE_HASH) throw Error('Production profile binding rejected');
  pi.registerProvider(config.provider_id,{baseUrl:config.endpoint,apiKey:'${ATO_PI_PROVIDER_KEY}',api:'ato-production-openai-v1',
    models:[{id:config.model_id,name:config.model_id,reasoning:false,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:config.input_tokens,maxTokens:config.output_tokens}],
    streamSimple(model,context,options){
      const stream=new Stream();
      (async()=>{try{
        let p=payload(config,context);p=await options?.onPayload?.(p,model)??p;
        const body=JSON.stringify(p);
        // UTF-8 bytes plus a conservative framing reserve upper-bound admitted input.
        if(Buffer.byteLength(body)+512>config.input_tokens||p.model!==config.model_id
            ||p.max_tokens!==config.output_tokens||p.stream!==true) throw Error('Production input bound');
        const guard=await import('./pi-coordinator-guard.mjs');
        const grant=await guard.authorizeAction('provider_request');
        const events=await request({...config,proxy_port:Number(process.env.ATO_PI_PROXY_PORT)},body,options?.apiKey,grant,
          {signal:options?.signal,onResponse:options?.onResponse});
        stream.message=decode(events,model,options?.apiKey);
      }catch{
        stream.message={role:'assistant',api:model.api,provider:model.provider,model:model.id,content:[],
          stopReason:'error',errorMessage:'Production request failed',timestamp:Date.now(),
          usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}};
      }finally{stream.finish();}})();return stream;
    }});
}
