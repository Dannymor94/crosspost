/* Presentation-only editor preview. Never sends, saves, resolves URLs or adds targets. */
(() => {
  "use strict";
  function describe({text="",title="",article="",media="none",targets=[],targetState="loading",articleMode=false}) {
    const labels={none:"Без вложения",image:"Фото",video:"Видео",file:"Вложение",missing:"Нужно выбрать вложение заново"};
    const names=targetState==="ready" ? targets.map(t=>String(t.title||t.platform_title||t.channel||"Канал")) : [];
    return {text:String(text),title:articleMode?String(title):"",article:articleMode?String(article):"",
      media:labels[media]||labels.none,names,targetState,
      articleNote:articleMode?"Статья и анонс могут отличаться в зависимости от площадки.":""};
  }
  function mount(root,data) {
    const value=describe(data);
    const heading=document.createElement("summary");
    heading.textContent="Предпросмотр публикации";
    const content=document.createElement("div");
    content.className="cp-stack cp-editor-preview-body";
    function add(tag,text,cls) {
      const node=document.createElement(tag);node.textContent=text;
      if(cls) node.className=cls;
      content.append(node);
    }
    add("p","Приблизительный вид. Оформление на площадках может отличаться.","cp-muted");
    if(value.title) add("h3",value.title);
    if(value.text) add("p",value.text,"cp-preview-text");
    if(value.article) add("p",value.article,"cp-preview-text");
    if(!value.text&&!value.title&&!value.article) add("p","Текст пока не добавлен.","cp-muted");
    add("p",value.media);
    add("p",value.targetState!=="ready"?"Цели ещё не подтверждены":
      value.names.length?"Выбрано: "+value.names.join(", "):"Цели не выбраны");
    if(value.articleNote) add("p",value.articleNote,"cp-muted");
    root.replaceChildren(heading,content);
  }
  function usePreview({useRef,useEffect},data) {
    const ref=useRef(null);
    const snapshot=JSON.stringify(data);
    useEffect(()=>{if(ref.current) mount(ref.current,JSON.parse(snapshot));},[snapshot]);
    return ref;
  }
  window.CrosspostEditorPreview=Object.freeze({describe,mount,usePreview});
})();
