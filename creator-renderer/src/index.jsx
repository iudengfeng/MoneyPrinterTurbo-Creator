import React from 'react';
import {AbsoluteFill, Audio, Composition, Img, Loop, OffthreadVideo, Sequence, interpolate, registerRoot, spring, useCurrentFrame, useVideoConfig} from 'remotion';

const COLORS={clean:'#ffffff',bold:'#ffe34d',knowledge:'#8ad7ff',business:'#ede4ca'};
const GRADES={none:'none',warm:'sepia(0.09) saturate(1.06) brightness(1.015)',cool:'saturate(0.93) contrast(1.025) hue-rotate(4deg)',vivid:'saturate(1.15) contrast(1.07) brightness(1.025)'};


const measure=(text,size)=>{
  const canvas=document.createElement('canvas');
  const context=canvas.getContext('2d');
  context.font=`800 ${size}px "Microsoft YaHei", "Noto Sans CJK SC", sans-serif`;
  return context.measureText(text).width;
};
const wrapTitle=(text,size,maxWidth)=>{
  const lines=[];
  let current='';
  for(const char of String(text)) {
    if(char==='\n') {lines.push(current);current='';continue;}
    if(current && measure(current+char,size)>maxWidth) {lines.push(current);current='';}
    current+=char;
  }
  if(current) lines.push(current);
  return lines;
};
const fitTitle=(text,size,maxWidth,width)=>{
  let lines=wrapTitle(text,size,maxWidth);
  while(lines.length>4 && size>width*0.023) {size-=1;lines=wrapTitle(text,size,maxWidth);}
  return {text:lines.join('\n'),size};
};
const fitCaption=(text,size,maxWidth)=>{
  const widest=Math.max(...String(text).split('\n').map(line=>measure(line,size)),1);
  return Math.min(size,size*maxWidth/widest);
};

const PipLayer=({item})=>{
  const {width,height,fps}=useVideoConfig();
  const layerWidth=width*item.size;
  const layerHeight=Math.min(height*0.42,layerWidth*(item.height || 9)/(item.width || 16));
  const gap=width*0.045;
  const position=item.position || 'top-right';
  const box={position:'absolute',width:layerWidth,height:layerHeight,overflow:'hidden',borderRadius:14,border:'2px solid #ffffffcc',boxShadow:'0 8px 24px #0008'};
  if(position==='center') Object.assign(box,{left:(width-layerWidth)/2,top:(height-layerHeight)/2});
  else {
    box[position.includes('left')?'left':'right']=gap;
    box[position.startsWith('top')?'top':'bottom']=position.startsWith('top') ? height*0.19 : height*0.23;
  }
  const mediaStyle={width:'100%',height:'100%',objectFit:'contain',background:'#111111'};
  return <div style={box}>
    {item.kind==='image' ? <Img src={item.path} style={mediaStyle}/> : <Loop durationInFrames={Math.max(1,Math.floor(item.sourceDuration*fps))}><OffthreadVideo src={item.path} muted style={mediaStyle}/></Loop>}
  </div>;
};

const WorkspaceVideo=(props)=>{
  const frame=useCurrentFrame();
  const {fps,width,height}=useVideoConfig();
  const seconds=frame/fps;
  const caption=(props.captions || []).find(c=>seconds>=c.start && seconds<c.end);
  const entrance=spring({frame,fps,config:{damping:200}});
  const style=props.style || 'clean';
  const accent=COLORS[style] || COLORS.clean;
  const legacyPip=props.template==='pip' && props.image;
  const card=props.template==='cards';
  const titleLength=String(props.title || '').length;
  const titleSize=width*(titleLength>65 ? 0.042 : titleLength>35 ? 0.047 : 0.054);
  const title=React.useMemo(()=>fitTitle(props.title || '',titleSize,width*0.82,width),[props.title,titleSize,width]);
  const captionSize=caption ? fitCaption(caption.text,width*(props.subtitleStyle==='bold'?0.064:0.052),width*0.80) : width*0.052;
  const titleStyle={position:'absolute',top:height*0.047,left:'6%',right:'6%',padding:`${width*0.019}px ${width*0.025}px`,fontSize:title.size,fontWeight:800,lineHeight:1.38,borderRadius:style==='business'?4:14,
    background:style==='bold'?'#ffe34deF':style==='business'?'#111827c9':'#101828cf',color:style==='bold'?'#171717':'#ffffff',
    border:style==='business'?'1px solid #ede4caaa':undefined,borderLeft:style==='knowledge'?`${width*0.009}px solid ${accent}`:undefined,
    opacity:entrance,transform:`translateY(${interpolate(entrance,[0,1],[-20,0])}px)`};
  const textStyle={whiteSpace:'pre-wrap',overflowWrap:'anywhere'};
  return <AbsoluteFill style={{backgroundColor:style==='business'?'#161b22':'#10131b',fontFamily:'Microsoft YaHei, Noto Sans CJK SC, sans-serif',color:'white'}}>
    {legacyPip && <Img src={props.image} style={{width:'100%',height:'100%',objectFit:'cover'}}/>}
    <div style={legacyPip ? {position:'absolute',right:'5%',bottom:'22%',width:'38%',height:'35%',borderRadius:24,overflow:'hidden',border:'3px solid #ffffff',boxShadow:'0 8px 30px #0008'} : {position:'absolute',inset:card?'13% 5% 23%':0,overflow:'hidden',borderRadius:card?20:0}}>
      <OffthreadVideo src={props.video} muted={Boolean(props.silent || props.audio)} style={{width:'100%',height:'100%',objectFit:props.videoFit || 'contain',filter:GRADES[props.colorGrade] || GRADES.none}}/>
    </div>
    {!props.silent && props.audio && <Audio src={props.audio}/>}
    {!props.silent && props.bgm && <Audio src={props.bgm} volume={props.bgmVolume ?? 0.12} loop/>}
    {(props.pipItems || []).map((item,index)=><Sequence key={index} from={Math.round(item.start*fps)} durationInFrames={Math.max(1,Math.round(item.end*fps)-Math.round(item.start*fps))}><PipLayer item={item}/></Sequence>)}
    {props.title && <div style={titleStyle}><div style={textStyle}>{title.text}</div></div>}
    {caption && props.subtitleStyle!=='none' && <div style={{position:'absolute',bottom:height*0.105,left:'7%',right:'7%',display:'flex',justifyContent:'center'}}>
      <div style={{maxWidth:'100%',boxSizing:'border-box',background:props.subtitleStyle==='bold'?'#0009':'#000b',padding:`${width*0.015}px ${width*0.022}px`,borderRadius:props.subtitleStyle==='bold'?6:12,
        fontSize:captionSize,fontWeight:800,lineHeight:1.35,textAlign:'center',whiteSpace:'pre',
        color:props.subtitleStyle==='yellow' || props.subtitleStyle==='bold'?'#ffe34d':'#ffffff',textShadow:'0 2px 5px #000, 0 -1px 2px #000',
        borderBottom:style==='knowledge'?`3px solid ${accent}`:undefined}}>{caption.text}</div>
    </div>}
    {card && <div style={{position:'absolute',bottom:'3.5%',left:'6%',right:'6%',height:3,background:accent,opacity:0.7}}/>}
  </AbsoluteFill>;
};

const Root=()=> <Composition id="CreatorVideo" component={WorkspaceVideo} width={720} height={1280} fps={30} durationInFrames={90} defaultProps={{captions:[],template:'talking',style:'clean',subtitleStyle:'clean'}} calculateMetadata={({props})=>({width:props.width,height:props.height,fps:props.fps,durationInFrames:props.durationInFrames})}/>;
registerRoot(Root);
