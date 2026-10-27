(function(root) {
  function stats(values) {
    if (!values.length) return null;
    const mean = values.reduce((a,b)=>a+b,0)/values.length;
    const variance = values.reduce((a,b)=>a+(b-mean)**2,0)/values.length;
    const sorted = [...values].sort((a,b)=>a-b), n=sorted.length;
    return {mean,variance,std:Math.sqrt(variance),median:n%2?sorted[(n-1)/2]:(sorted[n/2-1]+sorted[n/2])/2};
  }
  function symlog(v, threshold) { return Math.sign(v)*Math.log1p(Math.abs(v)/threshold); }
  function windowSamples(samples, now, seconds) { return samples.filter(s=>s.t>=now-seconds && s.t<=now && typeof s.value==='number' && Number.isFinite(s.value)); }
  const api={stats,symlog,windowSamples};
  if (typeof module !== 'undefined') module.exports=api;
  root.IVMath=api;
})(globalThis);
