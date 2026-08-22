"""Render a stitch pattern to a PNG preview (thread drawn at true width)."""
import numpy as np, pystitch
from PIL import Image, ImageDraw

def preview(pat, colors, out_path, px_wide=1000, bg=(184,189,196)):
    xs=[s[0] for s in pat.stitches]; ys=[s[1] for s in pat.stitches]
    minx,maxx,miny,maxy=min(xs),max(xs),min(ys),max(ys)
    S=px_wide/max(maxx-minx, maxy-miny, 1)
    pad=int(px_wide*0.03)
    W=int((maxx-minx)*S)+2*pad; H=int((maxy-miny)*S)+2*pad
    img=Image.new('RGB',(max(W,10),max(H,10)),bg); d=ImageDraw.Draw(img)
    T=lambda x,y:((x-minx)*S+pad,(y-miny)*S+pad)
    lw=max(2,int(round(0.45*S*10)))
    ci=0; prev=None
    for x,y,c in pat.stitches:
        k=c&0xFF
        if k==pystitch.COLOR_CHANGE: ci+=1; prev=None; continue
        if k==pystitch.STITCH:
            if prev is not None:
                d.line([T(*prev),T(x,y)],fill=colors[min(ci,len(colors)-1)],width=lw)
            prev=(x,y)
        elif k==pystitch.JUMP: prev=(x,y)
        else: prev=None
    img.save(out_path)
    return out_path
