(function(){
  const mount=document.getElementById("mount");
  let cfg;
  try{cfg=JSON.parse(decodeURIComponent(location.hash.slice(1)));}catch{cfg=null;}
  if(!cfg||typeof cfg.public_key!=="string"||cfg.public_key.length<8||cfg.public_key.length>512||
     typeof cfg.assistant_id!=="string"||!/^[0-9a-fA-F-]{36}$/.test(cfg.assistant_id)){
    mount.innerHTML='<p class="error">Preview configuration is invalid. Close this panel and rebuild the preview.</p>';
    return;
  }
  history.replaceState(null,"",location.pathname);
  const script=document.createElement("script");
  script.src="https://unpkg.com/@vapi-ai/client-sdk-react/dist/embed/widget.umd.js";
  script.async=true;
  script.type="text/javascript";
  script.onload=function(){
    mount.innerHTML="";
    const widget=document.createElement("vapi-widget");
    widget.setAttribute("public-key",cfg.public_key);
    widget.setAttribute("assistant-id",cfg.assistant_id);
    widget.setAttribute("mode","voice");
    widget.setAttribute("theme","light");
    widget.setAttribute("size","full");
    widget.setAttribute("radius","large");
    widget.setAttribute("show-transcript","false");
    widget.setAttribute("main-label","Talk to the personalized test agent");
    widget.setAttribute("start-button-text","Start private voice test");
    widget.setAttribute("end-button-text","End test");
    mount.appendChild(widget);
  };
  script.onerror=function(){mount.innerHTML='<p class="error">Vapi browser controls could not load. No call was started.</p>';};
  document.head.appendChild(script);
})();