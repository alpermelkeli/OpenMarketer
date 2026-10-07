const $=id=>document.getElementById(id);
function R(el){const c=$('canvas').getBoundingClientRect(),r=el.getBoundingClientRect();return{l:r.left-c.left,t:r.top-c.top,r:r.right-c.left,b:r.bottom-c.top,cx:(r.left+r.right)/2-c.left,cy:(r.top+r.bottom)/2-c.top}}
function P(el,s,o=0){const r=R(el);return s==='b'?{x:r.cx+o,y:r.b,d:'v'}:s==='t'?{x:r.cx+o,y:r.t,d:'v'}:s==='l'?{x:r.l,y:r.cy+o,d:'h'}:{x:r.r,y:r.cy+o,d:'h'}}
function rounded(p,rad){let d=`M${p[0][0]} ${p[0][1]}`;for(let i=1;i<p.length-1;i++){const[a,b,c]=[p[i-1],p[i],p[i+1]];const l1=Math.hypot(b[0]-a[0],b[1]-a[1]),l2=Math.hypot(c[0]-b[0],c[1]-b[1]);const r=Math.min(rad,l1/2,l2/2);const p1=[b[0]-(b[0]-a[0])/l1*r,b[1]-(b[1]-a[1])/l1*r],p2=[b[0]+(c[0]-b[0])/l2*r,b[1]+(c[1]-b[1])/l2*r];d+=` L${p1[0]} ${p1[1]} Q${b[0]} ${b[1]} ${p2[0]} ${p2[1]}`}const e=p[p.length-1];return d+` L${e[0]} ${e[1]}`}
let svg;
function initSvg(){const c=$('canvas');svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.id='edges';svg.innerHTML=`<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="#111"/></marker></defs>`;c.appendChild(svg)}
function edge(a,b,o={}){a=typeof a==='string'?$(a):a;b=typeof b==='string'?$(b):b;const A=P(a,o.from||'b',o.oa||0),B=P(b,o.to||'t',o.ob||0);let p=[[A.x,A.y]];
 if(A.d==='v'&&B.d==='v'){if(Math.abs(A.x-B.x)>1){const my=o.my!==undefined?o.my:A.y+(B.y-A.y)*(o.mid??.5);p.push([A.x,my],[B.x,my])}}
 else if(A.d==='h'&&B.d==='h'){if(Math.abs(A.y-B.y)>1){const mx=o.mx!==undefined?o.mx:A.x+(B.x-A.x)*(o.mid??.5);p.push([mx,A.y],[mx,B.y])}}
 else if(A.d==='v'){p.push([A.x,B.y])}else{p.push([B.x,A.y])}
 p.push([B.x,B.y]);
 const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',rounded(p,9));path.setAttribute('fill','none');path.setAttribute('stroke','#111');path.setAttribute('stroke-width','1.7');if(o.dash)path.setAttribute('stroke-dasharray','7 5');if(o.arrow!==false)path.setAttribute('marker-end','url(#ah)');if(o.bi)path.setAttribute('marker-start','url(#ah)');svg.appendChild(path);
 if(o.label){let best=0,bi=0;for(let i=0;i<p.length-1;i++){const l=Math.hypot(p[i+1][0]-p[i][0],p[i+1][1]-p[i][1]);if(l>best){best=l;bi=i}}const x=(p[bi][0]+p[bi+1][0])/2+(o.ldx||0),y=(p[bi][1]+p[bi+1][1])/2+(o.ldy||0);label(o.label,x,y)}
 if(o.la)label(o.la,A.x+(A.d==='h'?(o.from==='l'?-16:16):0)+(o.lax||0),A.y+(A.d==='v'?(o.from==='t'?-14:14):0)+(o.lay||0));
 if(o.lb)label(o.lb,B.x+(B.d==='h'?(o.to==='l'?-16:16):0)+(o.lbx||0),B.y+(B.d==='v'?(o.to==='t'?-14:14):0)+(o.lby||0));}
function label(t,x,y){const d=document.createElement('div');d.className='elabel';d.innerHTML=t;d.style.left=x+'px';d.style.top=y+'px';$('canvas').appendChild(d)}
function place(id,l,t,w,h){const e=$(id);e.style.position='absolute';e.style.left=l+'px';e.style.top=t+'px';if(w)e.style.width=w+'px';if(h)e.style.minHeight=h+'px'}
function finish(){const c=$('canvas');document.body.dataset.w=Math.ceil(c.offsetWidth);document.body.dataset.h=Math.ceil(c.offsetHeight);document.title='done'}
function run(fn){document.fonts.ready.then(()=>{initSvg();fn();finish()})}
