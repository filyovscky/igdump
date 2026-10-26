document.getElementById('copy-report').addEventListener('click',async()=>{
  const area=document.getElementById('text-report'),status=document.getElementById('copy-status');
  try{await navigator.clipboard.writeText(area.value);status.textContent='Отчёт скопирован.';}
  catch(_){area.focus();area.select();let copied=false;try{copied=document.execCommand('copy');}catch(_){}status.textContent=copied?'Отчёт скопирован.':'Текст выделен. Нажмите Ctrl+C или Cmd+C.';}
});
