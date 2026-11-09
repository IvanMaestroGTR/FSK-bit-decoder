#!/usr/bin/env python3
"""TMID / FSK audio analyzer. Python 3 + Tkinter only."""
import csv, math, os, shutil, struct, subprocess, sys, wave, importlib.util, traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

def load_wav(path):
    with wave.open(path, "rb") as w:
        ch, width, rate, count = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(count)
    if width == 1: vals = [(v-128)/128.0 for v in raw]
    elif width == 2: vals = [v[0]/32768.0 for v in struct.iter_unpack("<h",raw)]
    elif width == 3:
        vals=[]
        for i in range(0,len(raw),3):
            v=raw[i]|raw[i+1]<<8|raw[i+2]<<16
            if v&0x800000: v-=1<<24
            vals.append(v/8388608.0)
    elif width == 4: vals=[v[0]/2147483648.0 for v in struct.iter_unpack("<i",raw)]
    else: raise ValueError("Unsupported PCM WAV sample width")
    if ch>1: vals=[sum(vals[i:i+ch])/ch for i in range(0,len(vals),ch)]
    if vals:
        dc=sum(vals)/len(vals); vals=[v-dc for v in vals]   # remove DC offset
    return vals,rate,ch,width,count

def goertzel(x,rate,freq):
    n=len(x)
    if n<8 or freq<=0 or freq>=rate/2:return 0.0
    mean=sum(x)/n; s1=s2=0.0; coeff=2*math.cos(2*math.pi*freq/rate); norm=0.0
    for i,v in enumerate(x):
        win=0.5-0.5*math.cos(2*math.pi*i/(n-1))
        v=(v-mean)*win; norm+=v*v
        s=v+coeff*s1-s2; s2,s1=s1,s
    return max(0.0,s1*s1+s2*s2-coeff*s1*s2)/(norm+1e-15)

def auto_pair(samples,rate):
    """Find two recurring FSK tones from short overlapping windows, not whole-file energy."""
    if len(samples)<max(64,int(rate*0.025)):
        raise ValueError("Recording is too short for automatic tone detection")
    win=max(64,int(rate*0.020))       # 20 ms: enough cycles to separate common radio FSK pairs
    hop=max(1,win//2)
    freqs=range(600,3001,10)
    votes={}
    windows=0
    for start in range(0,max(1,len(samples)-win+1),hop):
        x=samples[start:start+win]
        if len(x)<win: break
        spectrum=[(goertzel(x,rate,f),f) for f in freqs]
        spectrum.sort(reverse=True)
        if not spectrum or spectrum[0][0]<=1e-10: continue
        peak=spectrum[0][0]
        selected=[]
        for power,f in spectrum:
            if power < peak*0.30: break
            if all(abs(f-g)>=180 for g in selected):
                selected.append(f)
            if len(selected)==2: break
        for f in selected:
            # Vote per window so a long constant preamble cannot swamp the data tones.
            bucket=int(round(f/20.0))*20
            votes[bucket]=votes.get(bucket,0)+1
        windows+=1
    ranked=sorted(votes.items(),key=lambda item:item[1],reverse=True)
    chosen=[]
    for f,count in ranked:
        if count < max(2,int(windows*0.04)): continue
        if all(abs(f-g)>=180 for g,_ in chosen):
            chosen.append((f,count))
        if len(chosen)==2: break
    if len(chosen)<2:
        raise ValueError("Auto detection could not find two recurring tones. Try selecting only the data burst or enter Mark and Space manually.")
    fs=sorted(f for f,_ in chosen)
    return fs[0],fs[1]

# Signal processing
def tone_prefix(x,rate,freq):
    """Running complex sum of x[n]*exp(-j*w*n): any window's tone magnitude is O(1)."""
    n=len(x); w=-2*math.pi*freq/rate
    step=complex(math.cos(w),math.sin(w)); ph=1+0j; acc=0j; out=[0j]*(n+1)
    for i in range(n):
        acc+=x[i]*ph; out[i+1]=acc; ph*=step
        if not i&2047: ph/=abs(ph)
    return out

def tone_mag(pref,a,b):
    return 2*abs(pref[b]-pref[a])/(b-a) if b-a>=4 else 0.0

def _pct(v,p):
    v=sorted(v); return v[min(len(v)-1,int(p*len(v)))] if v else 0.0

def estimate_levels(pm,ps,rate,baud):
    """Typical received amplitude of each tone (95th percentile of sliding 4-symbol windows)."""
    win=max(8,int(rate/baud*4)); hop=max(1,win//4); am=[];asp=[]
    for a in range(0,len(pm)-1-win,hop):
        am.append(tone_mag(pm,a,a+win)); asp.append(tone_mag(ps,a,a+win))
    return max(_pct(am,.95),1e-6),max(_pct(asp,.95),1e-6)

def two_means(v):
    if not v:return None
    lo,hi=min(v),max(v)
    if hi-lo<1e-9:return None
    c0,c1=lo,hi
    for _ in range(40):
        t=(c0+c1)/2; g0=[s for s in v if s<=t]; g1=[s for s in v if s>t]
        if not g0 or not g1:break
        n0,n1=sum(g0)/len(g0),sum(g1)/len(g1)
        if abs(n0-c0)<1e-9 and abs(n1-c1)<1e-9:break
        c0,c1=n0,n1
    return c0,c1

def separation(v):
    """Fisher ratio of the two score clusters: high when symbol timing is right."""
    tm=two_means(v)
    if not tm:return 0.0
    t=(tm[0]+tm[1])/2; g0=[s for s in v if s<=t]; g1=[s for s in v if s>t]
    if len(g0)<2 or len(g1)<2:return 0.0
    m0=sum(g0)/len(g0); m1=sum(g1)/len(g1)
    v0=sum((s-m0)**2 for s in g0)/len(g0); v1=sum((s-m1)**2 for s in g1)/len(g1)
    return (m1-m0)**2/(v0+v1+1e-6)

def symbol_scores(pm,ps,rate,period,offset,n,frac,am,asp,equalize):
    """score>0 leans Mark, <0 leans Space (tone magnitudes optionally level-equalised)."""
    out=[];N=len(pm)-1;half=period*frac*rate/2
    for i in range(n):
        c=(offset+(i+.5)*period)*rate; a=max(0,int(round(c-half))); b=min(N,int(round(c+half)))
        m=tone_mag(pm,a,b); s=tone_mag(ps,a,b)
        out.append((m/am-s/asp) if equalize else (m-s))
    return out

def active_span(x,rate,rel=0.3):
    """(start_s,end_s) of the part of the recording that actually has signal."""
    win=max(4,int(rate*0.0005))
    pk=[max(abs(v) for v in x[i:i+win]) for i in range(0,len(x),win)]
    if not pk:return 0.0,0.0
    th=rel*_pct(pk,.95); idx=[k for k,v in enumerate(pk) if v>=th]
    if not idx:return 0.0,len(x)/rate
    return idx[0]*win/rate,min(len(x),(idx[-1]+1)*win)/rate

def find_data_start(pm,ps,rate,baud,am,asp,t0,t1,long_syms=12):
    """End of the leading warble/long-tone train (runs >= long_syms symbols). Falls back to t0."""
    period=1/baud; hop=period/4; vals=[];times=[];t=t0
    while t+period<=t1:
        a=int(t*rate); b=int((t+period)*rate)
        vals.append(tone_mag(pm,a,b)/am-tone_mag(ps,a,b)/asp); times.append(t); t+=hop
    tm=two_means(vals)
    if not tm or tm[1]-tm[0]<0.3 or len(vals)<8:return t0
    thr=(tm[0]+tm[1])/2; dec=[v>thr for v in vals]
    runs_=[];i=0
    while i<len(dec):
        j=i
        while j<len(dec) and dec[j]==dec[i]:j+=1
        runs_.append((i,j));i=j
    end=t0;seen_long=False
    for i,j in runs_:
        dur=(j-1-i)*hop+period
        if dur>=long_syms*period: seen_long=True; end=times[j-1]+period
        elif seen_long and dur>=2*period:break      # first real data-length run after warble
    return end

def refine_offset(pm,ps,rate,baud,am,asp,approx,t1,frac,equalize,maxsym=400):
    """Slide symbol timing across one symbol, pick centre of the best (widest) plateau."""
    period=1/baud;res=[];steps=40
    for k in range(steps):
        off=approx-period/2+period*k/steps
        n=min(maxsym,int((t1-off)/period))
        if n<4:continue
        res.append((separation(symbol_scores(pm,ps,rate,period,off,n,frac,am,asp,equalize)),off))
    if not res:return approx
    mx=max(r[0] for r in res); k=max(range(len(res)),key=lambda i:res[i][0]); lo=hi=k
    while lo>0 and res[lo-1][0]>=.95*mx:lo-=1
    while hi<len(res)-1 and res[hi+1][0]>=.95*mx:hi+=1
    return (res[lo][1]+res[hi][1])/2

def crc16_modbus(data):
    crc=0xFFFF
    for b in data:
        crc^=b
        for _ in range(8): crc=(crc>>1)^(0xA001 if crc&1 else 0)
    return crc&0xFFFF

def runs(bits):
    if not bits:return []
    out=[]; val=bits[0]; count=1
    for b in bits[1:]:
        if b==val:count+=1
        else:out.append((val,count));val=b;count=1
    out.append((val,count));return out

def uart_candidates(bits,final_stop):
    found=[]
    for phase in range(min(10,len(bits))):
        p=phase; vals=[];bad=0
        while p+9+(1 if final_stop else 0)<=len(bits):
            if bits[p]==0:
                vals.append(sum(bits[p+1+j]<<j for j in range(8)))
                if final_stop and bits[p+9]!=1:bad+=1
                p+=10 if final_stop else 9
            else:p+=1
        if len(vals)>=3:found.append((bad,-len(vals),phase,vals))
    return sorted(found)

# ---------------------------------------------------------------------------
# Decoder plugin interface
#   A decoder module is any .py file that defines   decode(ctx)   and optionally   NAME = "..."
#   ctx keys:
#     bits        list[int]  data bits only (from the detected data start; = all_bits if none found)
#     all_bits    list[int]  every decoded symbol incl. warble/preamble
#     preamble_bits list[int]
#     data_idx    int        index in all_bits where data starts
#     rows        per-symbol tuples (idx, time_ms, tone_hz, bit, confidence, mark_score, space_score)
#     mark, space, baud, rate, offset_ms, polarity ("Mark=0"/"Mark=1"), final_stop (bool)
#     runs        helper: runs(bits) -> [(bit,count),...]        crc16_modbus(bytes) -> int
#   return a dict {"value": str, "debug": str}, or a (value, debug) tuple, or just a str (= value).
# ---------------------------------------------------------------------------
def builtin_decode(ctx):
    bits=ctx["bits"];dbg=[f"data bits: {len(bits)}","".join(map(str,bits)),""]
    cand=uart_candidates(bits,ctx["final_stop"])
    dbg.append("UART 8-N-1 LSB-first candidates (invalid_stop, bytes, phase):")
    for bad,neg,phase,vals in cand[:6]:dbg.append(f"  bad={bad} bytes={-neg} phase={phase}: "+" ".join(f"{v:02X}" for v in vals))
    if not cand:
        return {"value":"(no UART frame candidates found)","debug":"\n".join(dbg)}
    bad,neg,phase,vals=cand[0];val=[f"Best candidate: phase {phase}, {len(vals)} bytes, {bad} bad stop bit(s)","HEX  : "+" ".join(f"{v:02X}" for v in vals),
        "ASCII: "+"".join(chr(v) if 32<=v<127 else "." for v in vals)]
    if len(vals)>=9:
        payload=bytes(vals[:7]);got=vals[7]|(vals[8]<<8);calc=crc16_modbus(payload)
        val.append(f"TMID hypothesis: callsign={payload[:6]!r} SSID={payload[6:7]!r}")
        val.append(f"CRC16 received {got:04X}, calculated {calc:04X} -> {'VALID' if got==calc else 'INVALID'}")
    else:val.append("(fewer than 9 bytes: CRC check skipped)")
    return {"value":"\n".join(val),"debug":"\n".join(dbg)}

def normalise_decoder_result(res):
    if isinstance(res,dict):return str(res.get("value","")),str(res.get("debug",""))
    if isinstance(res,(tuple,list)) and len(res)>=2:return str(res[0]),str(res[1])
    return str(res),""

RATES=[8000,11025,16000,22050,32000,44100,48000,88200,96000,176400,192000,384000]

class App(tk.Tk):
    def __init__(self):
        super().__init__();self.title("TMID / FSK Bit & Preamble Analyzer");sw,sh=self.winfo_screenwidth(),self.winfo_screenheight()
        w0,h0=min(1280,sw-40),min(900,sh-110)
        self.geometry(f"{w0}x{h0}+{max(0,(sw-w0)//2)}+10");self.minsize(min(900,w0),min(520,h0))
        self.samples=None;self.rate=None;self.path=None;self.rows=[];self.results=[];self.meta={}
        self.rate_sel=tk.StringVar(value="Auto (from file)")
        self.file_rate=None
        self.decoders=[]
        self._ui()
        self.scan_decoders()

    def _ui(self):
        bar=ttk.Frame(self,padding=8);bar.pack(fill="x")
        ttk.Button(bar,text="Open PCM WAV…",command=self.open).pack(side="left")
        ttk.Label(bar,text="Sample rate (Hz)").pack(side="left",padx=(14,3))
        rc=ttk.Combobox(bar,textvariable=self.rate_sel,state="readonly",width=16,values=["Auto (from file)"]+[str(r) for r in RATES])
        rc.pack(side="left");rc.bind("<<ComboboxSelected>>",self._rate_changed)
        self.file_label=ttk.Label(bar,text="No recording loaded");self.file_label.pack(side="left",padx=10)
        cfg=ttk.LabelFrame(self,text="Signal settings",padding=8);cfg.pack(fill="x",padx=8,pady=4)
        self.mode=tk.StringVar(value="Auto");self.mark=tk.StringVar(value="1200");self.space=tk.StringVar(value="1800")
        self.baud=tk.StringVar(value="1200");self.offset=tk.StringVar(value="0");self.window=tk.StringVar(value="100");self.endms=tk.StringVar(value="")
        self.thrmode=tk.StringVar(value="Auto");self.keep_pre=tk.BooleanVar(value=True);self.data_start_ms=None;self.equalize=tk.BooleanVar(value=True)
        self.polarity=tk.StringVar(value="Mark=0");self.finalstop=tk.BooleanVar(value=False)
        fields=[("Frequency mode",self.mode,["Manual","Auto"]),("Mark (Hz)",self.mark,None),("Space (Hz)",self.space,None),
                ("Baud",self.baud,None),("Start offset (ms)",self.offset,None),("Window (%)",self.window,None),("End (ms, blank=auto)",self.endms,None)]
        for i,(label,var,choices) in enumerate(fields):
            col=i*2
            ttk.Label(cfg,text=label).grid(row=0,column=col,sticky="w",padx=(3,2))
            if choices: ttk.Combobox(cfg,textvariable=var,values=choices,state="readonly",width=9).grid(row=0,column=col+1,padx=(0,8))
            else: ttk.Entry(cfg,textvariable=var,width=9).grid(row=0,column=col+1,padx=(0,8))
        ttk.Label(cfg,text="Bit mapping").grid(row=1,column=0,sticky="w",pady=7)
        ttk.Combobox(cfg,textvariable=self.polarity,values=["Mark=0","Mark=1"],state="readonly",width=9).grid(row=1,column=1,sticky="w")
        ttk.Checkbutton(cfg,text="Expect final UART stop bit",variable=self.finalstop).grid(row=1,column=2,columnspan=2,sticky="w")
        ttk.Label(cfg,text="Threshold").grid(row=1,column=4,sticky="w")
        ttk.Combobox(cfg,textvariable=self.thrmode,values=["Auto","Zero"],state="readonly",width=9).grid(row=1,column=5,sticky="w")
        ttk.Checkbutton(cfg,text="Equalize tone levels",variable=self.equalize).grid(row=1,column=6,columnspan=2,sticky="w")
        ttk.Checkbutton(cfg,text="Include warble/preamble",variable=self.keep_pre).grid(row=2,column=0,columnspan=3,sticky="w")
        ttk.Button(cfg,text="Auto-sync (find data start)",command=self.auto_sync).grid(row=1,column=9,columnspan=3,sticky="e",padx=6)
        ttk.Button(cfg,text="Analyze",command=self.analyze).grid(row=1,column=12,columnspan=2,sticky="e")
        self.summary=ttk.Label(self,text="Load a WAV file. Warble/preamble is analyzed from the selected start offset.",padding=8);self.summary.pack(fill="x")
        split=ttk.Panedwindow(self,orient="vertical");split.pack(fill="both",expand=True,padx=8,pady=4)
        top=ttk.Frame(split);bottom=ttk.Frame(split);split.add(top,weight=2);split.add(bottom,weight=3)
        wave_frame=ttk.Frame(top);wave_frame.pack(fill="both",expand=True)
        ttk.Label(wave_frame,text="PCM waveform (full recording)").pack(anchor="w")
        self.wave_canvas=tk.Canvas(wave_frame,bg="#101820",height=95,highlightthickness=0)
        self.wave_canvas.pack(fill="both",expand=True)
        ttk.Label(wave_frame,text="Bit decisions").pack(anchor="w")
        self.bit_canvas=tk.Canvas(wave_frame,bg="#101820",height=75,highlightthickness=0)
        self.bit_canvas.pack(fill="both",expand=True)
        self.wave_canvas.bind("<Configure>",self._redraw_waveform)
        cols=("idx","ms","tone","bit","confidence","mark","space")
        self.table=ttk.Treeview(bottom,columns=cols,show="headings",height=5)
        for c,h,w in zip(cols,("Symbol","Time ms","Detected Hz","Bit","Confidence","Mark score","Space score"),(65,95,110,50,90,100,100)):
            self.table.heading(c,text=h);self.table.column(c,width=w,anchor="center")
        sy=ttk.Scrollbar(bottom,orient="vertical",command=self.table.yview);self.table.configure(yscrollcommand=sy.set)
        self.table.pack(side="left",fill="both",expand=True);sy.pack(side="right",fill="y")
        foot=ttk.Frame(self,padding=6);foot.pack(fill="x")
        ttk.Button(foot,text="Export FULL LOG (.txt)…",command=self.export_log).pack(side="left")
        ttk.Button(foot,text="Export bit CSV…",command=self.export_csv).pack(side="left",padx=6)
        ttk.Button(foot,text="Copy log",command=self.copy_log).pack(side="left")
        dbar=ttk.Frame(self,padding=(8,0));dbar.pack(fill="x")
        ttk.Label(dbar,text="Decoder:").pack(side="left")
        self.dec_combo=ttk.Combobox(dbar,state="readonly",width=52);self.dec_combo.pack(side="left",padx=6)
        self.dec_combo.bind("<<ComboboxSelected>>",lambda e:self.run_decoder())
        ttk.Button(dbar,text="Reload / refresh list",command=self.reload_decoder).pack(side="left")
        ttk.Button(dbar,text="Add module file…",command=self.add_decoder).pack(side="left",padx=4)
        ttk.Button(dbar,text="Open decoders folder",command=self.open_decoder_dir).pack(side="left")
        ttk.Button(dbar,text="Copy decoded",command=lambda:self._copy(self.val_text)).pack(side="right")
        ttk.Button(dbar,text="Copy debug",command=lambda:self._copy(self.dbg_text)).pack(side="right",padx=4)
        self.nb=ttk.Notebook(self)
        def tab(title):
            f=ttk.Frame(self.nb);t=tk.Text(f,height=7,wrap="word",font=("Consolas",9));sb=ttk.Scrollbar(f,orient="vertical",command=t.yview)
            t.configure(yscrollcommand=sb.set);t.pack(side="left",fill="both",expand=True);sb.pack(side="right",fill="y");self.nb.add(f,text=title);return t
        self.val_text=tab("Decoded value");self.dbg_text=tab("Debug");self.report=tab("Full log")
        self.nb.pack(side="bottom",fill="x",padx=8,pady=(2,8));dbar.pack_forget();dbar.pack(side="bottom",fill="x")
        foot.pack_forget();foot.pack(side="bottom",fill="x");split.pack_forget();split.pack(fill="both",expand=True,padx=8,pady=4)

    def open(self):
        p=filedialog.askopenfilename(filetypes=[("PCM WAV","*.wav"),("All files","*.*")])
        if not p:return
        try:
            self.samples,self.file_rate,ch,width,count=load_wav(p);self.rate=self.file_rate;self.path=p
            self.meta={"channels":ch,"sample_width_bytes":width,"frames":count}
            self._apply_rate();self._update_label()
            self.auto_sync()
        except Exception as e:messagebox.showerror("WAV error",str(e))

    def _apply_rate(self):
        sel=self.rate_sel.get()
        self.rate=self.file_rate if sel.startswith("Auto") else int(sel)

    def _update_label(self):
        fr=self.file_rate;note="" if self.rate==fr else f" (header says {fr} Hz, analysing as {self.rate} Hz)"
        self.file_label.config(text=f"{os.path.basename(self.path)} | {self.rate} Hz{note} | {self.meta.get('channels')} ch | {len(self.samples)/self.rate:.3f} s")

    def _rate_changed(self,event=None):
        if self.samples is None:return
        self._apply_rate();self._update_label();self.auto_sync()

    def _settings(self):
        self._apply_rate()
        baud=float(self.baud.get());offset=float(self.offset.get())/1000;pct=float(self.window.get())/100
        if baud<=0 or offset<0 or not .1<=pct<=1:raise ValueError("Check baud, offset, and window (10–100%).")
        if self.mode.get()=="Auto":
            mark,space=auto_pair(self.samples,self.rate);self.mark.set(f"{mark:.0f}");self.space.set(f"{space:.0f}")
        else:mark=float(self.mark.get());space=float(self.space.get())
        if mark<=0 or space<=0 or mark==space or max(mark,space)>=self.rate/2:raise ValueError("Mark and Space must be different positive frequencies below Nyquist.")
        a0,a1=active_span(self.samples,self.rate)
        e=self.endms.get().strip();end_t=float(e)/1000 if e else a1
        return baud,offset,pct,mark,space,a0,min(end_t,len(self.samples)/self.rate)

    def _prep(self,mark,space,baud):
        key=(id(self.samples),self.rate,mark,space)
        if getattr(self,"_pkey",None)!=key:
            self._pm=tone_prefix(self.samples,self.rate,mark);self._ps=tone_prefix(self.samples,self.rate,space);self._pkey=key
        if self.equalize.get():am,asp=estimate_levels(self._pm,self._ps,self.rate,baud)
        else:am=asp=1.0
        return self._pm,self._ps,am,asp

    def auto_sync(self):
        if self.samples is None:messagebox.showinfo("No WAV","Open a recording first.");return
        try:
            baud,offset,pct,mark,space,a0,a1=self._settings()
            pm,ps,am,asp=self._prep(mark,space,baud)
            approx=find_data_start(pm,ps,self.rate,baud,am,asp,a0,a1)
            best=refine_offset(pm,ps,self.rate,baud,am,asp,approx,a1,pct,self.equalize.get())
            start=best
            if self.keep_pre.get():   # same symbol phase, but begin at the first sound so the warble/preamble is decoded too
                period=1/baud;k=int(max(0,best-a0)//period);start=best-k*period
            self.offset.set(f"{max(0,start)*1000:.3f}");self.data_start_ms=approx*1000
            self.sync_note=f"Auto-sync: signal starts {a0*1000:.2f} ms, data starts ≈ {approx*1000:.2f} ms, symbol phase from {best*1000:.3f} ms; analysis begins {start*1000:.3f} ms"
        except Exception as e:messagebox.showerror("Auto-sync",str(e));return
        self.analyze()

    def analyze(self):
        if self.samples is None:messagebox.showinfo("No WAV","Open a recording first.");return
        try:baud,offset,pct,mark,space,a0,end_t=self._settings()
        except Exception as e:messagebox.showerror("Settings / auto detect",str(e));return
        period=1/baud;n=max(0,int((end_t-offset)/period))
        pm,ps,am,asp=self._prep(mark,space,baud)
        eq=self.equalize.get()
        scores=symbol_scores(pm,ps,self.rate,period,offset,n,pct,am,asp,eq)
        tm=two_means(scores) if self.thrmode.get()=="Auto" else None
        if tm and tm[1]-tm[0]>=0.3:thr=(tm[0]+tm[1])/2;half=(tm[1]-tm[0])/2
        else:thr=0.0;half=1.0
        bits=[];rows=[];sep=separation(scores);halfw=period*pct*self.rate/2
        for i,sc in enumerate(scores):
            center=offset+(i+.5)*period;c=center*self.rate
            a=max(0,int(round(c-halfw)));b=min(len(self.samples),int(round(c+halfw)))
            vm=tone_mag(pm,a,b)/am;vs=tone_mag(ps,a,b)/asp
            mk=sc>thr;bit=0 if mk else 1
            if self.polarity.get()=="Mark=1":bit=1-bit
            conf=min(1.0,abs(sc-thr)/half)
            bits.append(bit);rows.append((i,center*1000,mark if mk else space,bit,conf,vm,vs))
        self.rows=rows;self.results={"mark":mark,"space":space,"baud":baud,"offset_ms":offset*1000,"window_pct":pct*100,"bits":bits,
                                      "data_idx":(None if self.data_start_ms is None else max(0,int(round((self.data_start_ms/1000-offset)/period)))),"levels":(am,asp),"thr":thr,"sep":sep,"end_ms":end_t*1000}
        self.table.delete(*self.table.get_children())
        for r in rows:self.table.insert("","end",values=(r[0],f"{r[1]:.3f}",f"{r[2]:.1f}",r[3],f"{r[4]:.3f}",f"{r[5]:.3f}",f"{r[6]:.3f}"))
        self.draw_waveform();self.draw(rows,mark,space);self.make_report()
        avg=sum(r[4] for r in rows)/max(1,len(rows))
        self.summary.config(text=f"{len(rows)} symbols | Mark {mark:g} Hz / Space {space:g} Hz | {baud:g} baud | offset {offset*1000:.2f} ms | mean confidence {avg:.3f} | cluster separation {sep:.1f}")

    def _redraw_waveform(self,event=None):
        if self.samples is not None:self.draw_waveform()

    def draw_waveform(self):
        c=self.wave_canvas;c.delete("all")
        w=max(1,c.winfo_width());h=max(1,c.winfo_height())
        if not self.samples:return
        left,right,top,bottom=48,w-12,12,h-25
        plot_w=max(1,right-left);plot_h=max(1,bottom-top)
        mid=top+plot_h/2
        # Horizontal amplitude grid and labels.
        for amp in (-1.0,-0.5,0.0,0.5,1.0):
            y=mid-amp*(plot_h/2)
            c.create_line(left,y,right,y,fill="#344552" if amp else "#718392",dash=(2,3) if amp else ())
            c.create_text(left-7,y,text=f"{amp:g}",anchor="e",fill="#c6d0d8",font=("TkDefaultFont",8))
        # Min/max decimation keeps narrow pulses visible without drawing every PCM sample.
        total=len(self.samples)
        for px in range(plot_w):
            a=int(px*total/plot_w);b=max(a+1,int((px+1)*total/plot_w))
            chunk=self.samples[a:min(total,b)]
            if not chunk:continue
            lo=min(chunk);hi=max(chunk)
            y1=mid-hi*(plot_h/2);y2=mid-lo*(plot_h/2)
            x=left+px
            c.create_line(x,y1,x,y2,fill="#40d6a2",width=1)
        duration=total/self.rate
        for tick in range(6):
            x=left+plot_w*tick/5
            c.create_line(x,top,x,bottom,fill="#263641",dash=(2,4))
            c.create_text(x,bottom+4,text=f"{duration*tick/5*1000:.0f} ms",anchor="n",fill="#c6d0d8",font=("TkDefaultFont",8))
        c.create_text(5,3,anchor="nw",fill="white",text=f"Amplitude | {duration*1000:.1f} ms total")

    def draw(self,rows,mark,space):
        self.bit_canvas.delete("all");w=max(1,self.bit_canvas.winfo_width());h=max(1,self.bit_canvas.winfo_height())
        if not rows:return
        self.bit_canvas.create_text(10,8,anchor="nw",fill="white",text="Symbol decisions from selected start offset (includes warble if offset starts at recording beginning)")
        step=max(1,(w-30)/len(rows));top=30;usable=h-55
        for r in rows:
            x=15+r[0]*step;y=top if r[3]==0 else top+usable*.55;color="#40d6a2" if r[3]==0 else "#ffb454"
            self.bit_canvas.create_rectangle(x,y,x+max(1,step-1),y+usable*.38,fill=color,outline="")
            if step>12:self.bit_canvas.create_text(x+step/2,y+usable*.19,text=str(r[3]),fill="#101820")
        di=self.results.get("data_idx")
        if di is not None and 0<di<len(rows):
            xm=15+di*step;self.bit_canvas.create_line(xm,22,xm,h-4,fill="#ff5c5c",width=2,dash=(4,3))
            self.bit_canvas.create_text(xm+4,h-10,anchor="w",fill="#ff9a9a",text=f"data starts (symbol {di}) — left of this line is warble/preamble")

    # ----- decoder plugin handling (menu is built from the "decoders" folder next to this script)
    def decoders_dir(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),"decoders")

    @staticmethod
    def _load_module(path):
        spec=importlib.util.spec_from_file_location("tmid_dec_"+str(abs(hash(path))),path)
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        if not callable(getattr(mod,"decode",None)):raise ValueError("Module has no decode(ctx) function.")
        return str(getattr(mod,"NAME",os.path.splitext(os.path.basename(path))[0])),mod.decode

    def scan_decoders(self,keep_path=None):
        entries=[{"label":"TMID – built-in (UART 8-N-1 + CRC16 hypothesis)","func":builtin_decode,"path":None,"error":None}]
        d=self.decoders_dir()
        try:os.makedirs(d,exist_ok=True);files=sorted(f for f in os.listdir(d) if f.lower().endswith(".py") and not f.startswith("_"))
        except OSError:files=[]
        for fn in files:
            p=os.path.join(d,fn)
            try:name,func=self._load_module(p);entries.append({"label":name,"func":func,"path":p,"error":None})
            except Exception:entries.append({"label":f"{fn}  (failed to load – select to see why)","func":None,"path":p,"error":traceback.format_exc()})
        prev=self.decoders[self.dec_combo.current()] if self.decoders and self.dec_combo.current()>=0 else None
        self.decoders=entries;self.dec_combo["values"]=[e["label"] for e in entries]
        pick=0
        for k,e in enumerate(entries):
            if keep_path and e["path"]==keep_path:pick=k;break
            if not keep_path and prev and e["path"]==prev["path"] and e["path"] is not None:pick=k;break
        self.dec_combo.current(pick)

    def reload_decoder(self):
        self.scan_decoders();self.run_decoder()

    def add_decoder(self):
        p=filedialog.askopenfilename(title="Select decoder module (.py)",filetypes=[("Python decoder","*.py"),("All files","*.*")])
        if not p:return
        d=self.decoders_dir();os.makedirs(d,exist_ok=True);dest=os.path.join(d,os.path.basename(p))
        try:
            if os.path.abspath(p)!=os.path.abspath(dest):shutil.copyfile(p,dest)
        except Exception as e:messagebox.showerror("Add module",str(e));return
        self.scan_decoders(keep_path=dest);self.run_decoder()

    def open_decoder_dir(self):
        d=self.decoders_dir();os.makedirs(d,exist_ok=True)
        try:
            if sys.platform.startswith("win"):os.startfile(d)
            elif sys.platform=="darwin":subprocess.Popen(["open",d])
            else:subprocess.Popen(["xdg-open",d])
        except Exception:messagebox.showinfo("Decoders folder",d)

    def _show_decoder(self,value,debug):
        for t,txt in ((self.val_text,value),(self.dbg_text,debug)):t.delete("1.0","end");t.insert("end",txt)

    def _copy(self,widget):
        self.clipboard_clear();self.clipboard_append(widget.get("1.0","end").rstrip())

    def run_decoder(self):
        e0=self.decoders[max(0,self.dec_combo.current())] if self.decoders else None
        if e0 is not None and e0["func"] is None:self._show_decoder("This decoder module failed to load (see Debug tab).",e0["error"] or "");return
        if not self.results or "bits" not in self.results:
            self._show_decoder("Run an analysis first.","");return
        z=self.results;allb=z["bits"];di=z.get("data_idx") or 0
        ctx={"bits":allb[di:],"all_bits":allb,"preamble_bits":allb[:di],"data_idx":di,"rows":self.rows,"mark":z["mark"],"space":z["space"],
             "baud":z["baud"],"rate":self.rate,"offset_ms":z["offset_ms"],"polarity":self.polarity.get(),"final_stop":self.finalstop.get(),
             "runs":runs,"crc16_modbus":crc16_modbus}
        e=self.decoders[max(0,self.dec_combo.current())]
        if e["func"] is None:self._show_decoder("This decoder module failed to load (see Debug tab).",e["error"] or "");return
        try:value,debug=normalise_decoder_result(e["func"](ctx))
        except Exception:value,debug="Decoder raised an exception (see Debug tab).",traceback.format_exc()
        self._show_decoder(value,debug)

    def make_report(self):
        z=self.results;bits=z["bits"];rr=runs(bits);avg=sum(r[4] for r in self.rows)/max(1,len(self.rows))
        lines=["TMID / FSK ANALYSIS LOG",f"Input: {self.path}",f"Sample rate: {self.rate} Hz",f"Channels: {self.meta.get('channels')}",f"Sample width: {self.meta.get('sample_width_bytes')} bytes",
               f"Duration: {len(self.samples)/self.rate:.6f} s",f"Mark: {z['mark']} Hz",f"Space: {z['space']} Hz",f"Baud: {z['baud']}",f"Start offset: {z['offset_ms']} ms",
               f"Analysis window: {z['window_pct']}%",f"Mapping: {self.polarity.get()}",f"Mean confidence: {avg:.4f}",f"Tone levels (mark/space amplitude): {z['levels'][0]:.4f} / {z['levels'][1]:.4f}",f"Decision threshold: {z['thr']:.3f}   cluster separation (Fisher): {z['sep']:.1f}",f"Analysis end: {z['end_ms']:.2f} ms",getattr(self,"sync_note",""),"","BIT STREAM (chronological):","".join(map(str,bits)),"",
               (f"Data starts at symbol {z['data_idx']} (preamble/warble = symbols 0..{z['data_idx']-1})" if z.get("data_idx") else "Data start: not detected"),"PREAMBLE / WARBLE RUN-LENGTH VIEW (bit, symbol-count):"," ".join(f"{v}:{n}" for v,n in rr),
               "RUN-LENGTH ONLY:"," ".join(str(n) for _,n in rr),"",
               "RAW 8-BIT LSB-FIRST GROUPS:"," ".join(f"{b:02X}" for b in [sum(bits[i+j]<<j for j in range(8)) for i in range(0,len(bits)-7,8)]),"",
               "PER-SYMBOL DETAIL:","index,time_ms,tone_hz,bit,confidence,mark_score,space_score"]
        lines += [f"{r[0]},{r[1]:.4f},{r[2]:.1f},{r[3]},{r[4]:.5f},{r[5]:.5f},{r[6]:.5f}" for r in self.rows]
        lines += ["","UART FRAMING CANDIDATES:"]
        cand=uart_candidates(bits,self.finalstop.get())
        if cand:
            for bad,neg,phase,vals in cand[:5]:
                lines.append(f"phase={phase}; bytes={len(vals)}; invalid_stop={bad}; hex="+" ".join(f"{v:02X}" for v in vals))
                if len(vals)>=9:
                    payload=bytes(vals[:7]);got=vals[7]|(vals[8]<<8);calc=crc16_modbus(payload)
                    lines.append(f"TMID hypothesis callsign={payload[:6]!r} SSID={payload[6:7]!r}; CRC received={got:04X} calculated={calc:04X} {'VALID' if got==calc else 'INVALID'}")
        else:lines.append("No UART candidate found.")
        lines += ["","NOTE: Audio-side analysis only. It does not emulate BK4819 sync detection, FIFO packing, or IRQ behavior."]
        self.log_text="\n".join(lines)
        self.report.delete("1.0","end");self.report.insert("end",self.log_text);self.run_decoder()

    def export_log(self):
        if not self.results:return
        p=filedialog.asksaveasfilename(defaultextension=".txt",filetypes=[("Text log","*.txt")],initialfile="tmid_fsk_analysis.txt")
        if not p:return
        try:
            with open(p,"w",encoding="utf-8",newline="\n") as f:f.write(self.log_text+"\n")
            messagebox.showinfo("Log exported",p)
        except Exception as e:messagebox.showerror("Export failed",str(e))

    def export_csv(self):
        if not self.rows:return
        p=filedialog.asksaveasfilename(defaultextension=".csv",filetypes=[("CSV","*.csv")],initialfile="tmid_fsk_bits.csv")
        if not p:return
        with open(p,"w",newline="",encoding="utf-8") as f:
            w=csv.writer(f);w.writerow(["symbol_index","time_ms","detected_tone_hz","bit","confidence","mark_score","space_score"]);w.writerows(self.rows)
        messagebox.showinfo("CSV exported",p)

    def copy_log(self):
        if not self.results:return
        self.clipboard_clear();self.clipboard_append(self.log_text);messagebox.showinfo("Copied","Full diagnostic log copied.")

if __name__=="__main__":App().mainloop()