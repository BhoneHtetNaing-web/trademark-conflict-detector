from pathlib import Path
import csv, hashlib, json, re, traceback, zipfile, io, os, math, shutil, subprocess, tempfile
import pymupdf
import cv2
import numpy as np
from PIL import Image, ImageDraw
from openpyxl import load_workbook, Workbook
from openpyxl.styles import Font
from skimage.metrics import structural_similarity as ssim
import xml.etree.ElementTree as ET

APP_RE = re.compile(r'(?<![A-Z0-9])(?:[A-Z]{1,8}/)?T\s*/\s*(\d{4})\s*/\s*(\d{3,10})(?!\d)', re.I)
OCR_APP_RE = re.compile(r'(?<![A-Z0-9])(?:[A-Z]{1,8}/)?[T7I]\s*/\s*(\d{4})\s*/\s*(\d{3,10})(?!\d)', re.I)
MEDIA_EXT = {'.png','.jpg','.jpeg','.bmp','.webp','.tif','.tiff','.gif','.wmf','.emf','.svg'}
MARK_WORDS = {'MARK','LOGO','DEVICE','TRADEMARK','TRADEMARKS','LABEL','BRAND','FIGURE'}


def norm_app(v):
    if v is None: return None
    text=str(v).upper()
    m = re.search(r'T\s*/\s*(\d{4})\s*/\s*(\d{3,10})', text)
    if not m:
        # OCR commonly reads the capital T as 7 or I. This normalization is
        # used only after OCR extraction; source text matching remains strict T/.
        m = re.search(r'[T7I]\s*/\s*(\d{4})\s*/\s*(\d{3,10})', text)
    return f'T/{m.group(1)}/{m.group(2)}' if m else None


def norm_text(v):
    if v is None: return ''
    return re.sub(r'[^A-Z0-9]+', '', str(v).upper())


def sha_bytes(b): return hashlib.sha256(b).hexdigest()


def sha(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for b in iter(lambda:f.read(1048576),b''): h.update(b)
    return h.hexdigest()


def render(doc,i,out,dpi=220):
    p=out/f'page_{i+1:04d}.png'
    pix=doc[i].get_pixmap(matrix=pymupdf.Matrix(dpi/72,dpi/72),alpha=False)
    pix.save(str(p)); return p


def page_words(page):
    try:
        return [tuple(w) for w in page.get_text('words')]
    except Exception:
        return []


def app_bbox_from_text(page, app_no):
    target=norm_text(app_no)
    words=page_words(page)
    for w in words:
        raw=str(w[4])
        if target and target in norm_text(raw): return tuple(w[:4])
    for i,w in enumerate(words):
        joined=''; x0=y0=1e9; x1=y1=0
        for j in range(i,min(i+12,len(words))):
            ww=words[j]; joined += str(ww[4]); x0=min(x0,ww[0]); y0=min(y0,ww[1]); x1=max(x1,ww[2]); y1=max(y1,ww[3])
            if target and target in norm_text(joined): return (x0,y0,x1,y1)
    return None


def ocr_words(page, scale=2.5):
    import pytesseract
    pix=page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False)
    im=Image.open(io.BytesIO(pix.tobytes('png')))
    data=pytesseract.image_to_data(im, config='--psm 6', output_type=pytesseract.Output.DICT)
    tokens=[]
    for i,txt in enumerate(data['text']):
        txt=str(txt or '').strip()
        if not txt: continue
        x,y,w,h=[int(data[k][i]) for k in ('left','top','width','height')]
        conf=float(data.get('conf',['-1'])[i]) if data.get('conf') else -1
        tokens.append((txt,x/scale,y/scale,(x+w)/scale,(y+h)/scale,conf))
    return tokens


def ocr_page(page, scale=2.5):
    tokens=ocr_words(page,scale)
    found=[]
    for i,t in enumerate(tokens):
        joined=''; box=None
        for j in range(i,min(i+14,len(tokens))):
            tx,x0,y0,x1,y1,*_=tokens[j]; joined += tx
            box=(x0,y0,x1,y1) if box is None else (min(box[0],x0),min(box[1],y0),max(box[2],x1),max(box[3],y1))
            m=OCR_APP_RE.search(joined)
            if m:
                found.append((m.group(0),box)); break
    return found


def pdf_records(doc,work,progress):
    rec=[]
    for i,page in enumerate(doc):
        text=page.get_text('text')
        matches=list(APP_RE.finditer(text))
        if matches:
            for m in matches:
                n=norm_app(m.group(0)); rec.append({'page':i+1,'application_raw':m.group(0),'application_no':n,'app_bbox':app_bbox_from_text(page,n),'source':'text'})
        else:
            try:
                for raw,bbox in ocr_page(page):
                    n=norm_app(raw)
                    if n: rec.append({'page':i+1,'application_raw':raw,'application_no':n,'app_bbox':bbox,'source':'ocr'})
            except Exception:
                pass
        progress(i+1,len(doc),len(rec))
    uniq={}
    for r in rec:
        k=(r['page'],r['application_no'])
        if k not in uniq or (uniq[k].get('app_bbox') is None and r.get('app_bbox') is not None): uniq[k]=r
    return list(uniq.values())


def cv(data):
    try:
        if isinstance(data,(bytes,bytearray)): return cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
        return cv2.imdecode(np.fromfile(str(data),np.uint8),cv2.IMREAD_COLOR)
    except Exception: return None


def render_vector_bytes(data, ext):
    suffix=ext.lower() if ext.startswith('.') else '.'+ext.lower()
    with tempfile.TemporaryDirectory() as td:
        src=Path(td)/('asset'+suffix); dst=Path(td)/'asset.png'
        src.write_bytes(data)
        magick=shutil.which('magick') or '/opt/imagemagick/bin/magick'
        commands=[]
        if magick:
            commands.append([magick,'-density','300',str(src),'-background','white','-alpha','remove','-alpha','off',str(dst)])
        ink=shutil.which('inkscape')
        if ink and suffix in {'.svg','.emf'}:
            commands.append([ink,str(src),'--export-type=png','--export-filename='+str(dst)])
        for cmd in commands:
            try:
                p=subprocess.run(cmd,capture_output=True,text=True,timeout=40)
                if p.returncode==0 and dst.exists():
                    im=cv(dst)
                    if im is not None:return im
            except Exception: pass
    return None


def _foreground_mask(g):
    g=cv2.normalize(g,None,0,255,cv2.NORM_MINMAX)
    blur=cv2.GaussianBlur(g,(5,5),0)
    _,th=cv2.threshold(blur,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    # Remove long page/table rules. This is deliberately conservative.
    h,w=th.shape
    hk=cv2.getStructuringElement(cv2.MORPH_RECT,(max(20,w//12),1))
    vk=cv2.getStructuringElement(cv2.MORPH_RECT,(1,max(20,h//12)))
    horiz=cv2.morphologyEx(th,cv2.MORPH_OPEN,hk)
    vert=cv2.morphologyEx(th,cv2.MORPH_OPEN,vk)
    th=cv2.bitwise_and(th,cv2.bitwise_not(cv2.bitwise_or(horiz,vert)))
    kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(3,3))
    th=cv2.morphologyEx(th,cv2.MORPH_OPEN, kernel)
    th=cv2.morphologyEx(th,cv2.MORPH_CLOSE, kernel)
    return th



def _logo_mask(g):
    """Logo-only foreground mask. Unlike page-mask extraction, never removes long lines: a trademark may itself be a circle, underline, or frame."""
    g=cv2.normalize(g,None,0,255,cv2.NORM_MINMAX)
    blur=cv2.GaussianBlur(g,(3,3),0)
    mean=float(np.mean(blur))
    _,th=cv2.threshold(blur,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)
    if mean < 127:
        th=cv2.bitwise_not(th)
    th=cv2.morphologyEx(th,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(3,3)))
    return th

def canonical(img,size=512):
    if img is None:return None
    g=cv2.cvtColor(img,cv2.COLOR_BGR2GRAY) if len(img.shape)==3 else img.copy()
    mask=_logo_mask(g)
    n,labels,stats,_=cv2.connectedComponentsWithStats(mask,8)
    clean=np.zeros_like(mask)
    min_area=max(8,int(mask.size*0.00004))
    for i in range(1,n):
        area=stats[i,cv2.CC_STAT_AREA]
        if area>=min_area: clean[labels==i]=255
    ys,xs=np.where(clean>0)
    if len(xs)>20:
        pad=max(4,int(min(g.shape)*0.025)); x0=max(0,xs.min()-pad); x1=min(g.shape[1],xs.max()+pad+1); y0=max(0,ys.min()-pad); y1=min(g.shape[0],ys.max()+pad+1)
        g=g[y0:y1,x0:x1]
    g=cv2.resize(g,(size,size),interpolation=cv2.INTER_AREA)
    # Standardize polarity so black-on-white and white-on-black logos compare identically.
    if float(np.mean(g))<127:g=255-g
    return g


def canonical_mask(img,size=512):
    g=canonical(img,size)
    if g is None:return None
    m=_foreground_mask(g)
    # Keep the dominant connected foreground; this avoids OCR/edge noise entering identity.
    # Keep all foreground components: many trademarks intentionally contain
    # disconnected letters/shapes. Do not reduce a multi-part logo to its
    # single largest component.
    return m


def contour_signature(img):
    if img is None:return None
    g=img if len(img.shape)==2 else cv2.cvtColor(img,cv2.COLOR_BGR2GRAY)
    th=_logo_mask(g)
    contours,_=cv2.findContours(th,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    H,W=g.shape[:2]; good=[]
    for c in contours:
        x,y,w,h=cv2.boundingRect(c); area=cv2.contourArea(c)
        if w*h < 0.003*W*H or w*h > 0.90*W*H: continue
        good.append((area,c))
    if not good:return None
    good.sort(key=lambda z:z[0],reverse=True); c=good[0][1]; x,y,w,h=cv2.boundingRect(c)
    hu=cv2.HuMoments(cv2.moments(c)).flatten(); hu=np.sign(hu)*np.log10(np.abs(hu)+1e-30)
    return {'bbox':[int(x),int(y),int(x+w),int(y+h)],'area_ratio':float(cv2.contourArea(c)/(W*H)),'hu':[round(float(v),8) for v in hu]}


def contour_similarity(a,b):
    ca,cb=contour_signature(a),contour_signature(b)
    if not ca or not cb:return 0.0
    ha=np.array(ca['hu']); hb=np.array(cb['hu'])
    hu_sim=float(max(0,1-np.mean(np.minimum(np.abs(ha-hb)/10,1))))
    ar=float(max(0,1-abs(ca['area_ratio']-cb['area_ratio'])/max(ca['area_ratio'],cb['area_ratio'],1e-9)))
    ba=np.array(ca['bbox'],float); bb=np.array(cb['bbox'],float); ba[2:]-=ba[:2]; bb[2:]-=bb[:2]
    shape_sim=float(max(0,1-np.mean(np.abs(ba-bb)/np.maximum(np.maximum(ba,bb),1))))
    return round(0.55*hu_sim+0.25*ar+0.20*shape_sim,4)


def _bits_hex(arr):
    bits=0
    for v in arr.flatten().astype(np.uint8): bits=(bits<<1)|int(v)
    width=(arr.size+3)//4
    return f'{bits:0{width}x}'


def _hamming_hex(a,b): return (int(a,16)^int(b,16)).bit_count()


def _image_hashes(g):
    small=cv2.resize(g,(32,32),interpolation=cv2.INTER_AREA).astype(np.float32); d=cv2.dct(small)[:8,:8]; vals=d.flatten()[1:]
    ph=_bits_hex((d[1:,:] > np.median(vals)).astype(np.uint8))
    dhg=cv2.resize(g,(9,8),interpolation=cv2.INTER_AREA); dh=_bits_hex((dhg[:,1:] > dhg[:,:-1]).astype(np.uint8))
    ahg=cv2.resize(g,(8,8),interpolation=cv2.INTER_AREA); ah=_bits_hex((ahg > ahg.mean()).astype(np.uint8))
    return ph,dh,ah


def fingerprints(img):
    g=canonical(img)
    if g is None:return {}
    ph,dh,ah=_image_hashes(g)
    mask=canonical_mask(img)
    return {'sha256':sha_bytes(g.tobytes()),'mask_sha256':sha_bytes(mask.tobytes()) if mask is not None else None,'phash':ph,'dhash':dh,'ahash':ah}


def aligned_mask_identity(a,b):
    ma,mb=canonical_mask(a),canonical_mask(b)
    if ma is None or mb is None:return False,0.0,0,0.0
    # Compare the complete normalized foreground shape under a small rotation
    # tolerance. Mask-SSIM is much more stable than raw pixel hashing for
    # scanned/resized copies of the same logo.
    best=0.0; best_angle=0; best_ssim=0.0
    for angle in (0,-2,2,-4,4):
        M=cv2.getRotationMatrix2D((256,256),angle,1.0); rb=cv2.warpAffine(mb,M,(512,512),borderValue=0)
        inter=np.logical_and(ma>0,rb>0).sum(); union=np.logical_or(ma>0,rb>0).sum()
        iou=float(inter/max(union,1)); ms=float(ssim(ma,rb,data_range=255))
        if iou>best or (abs(iou-best)<1e-9 and ms>best_ssim): best=iou; best_angle=angle; best_ssim=ms
    return bool(best>=0.92 and best_ssim>=0.995),best,best_angle,best_ssim


def score(a,b):
    if a is None or b is None:return 0.0,{},False
    ga,gb=canonical(a),canonical(b)
    if ga is None or gb is None:return 0.0,{},False
    fa,fb=fingerprints(a),fingerprints(b)
    exact=fa['sha256']==fb['sha256']; mask_exact=fa['mask_sha256']==fb['mask_sha256']
    ph=1-_hamming_hex(fa['phash'],fb['phash'])/56; dh=1-_hamming_hex(fa['dhash'],fb['dhash'])/64; ah=1-_hamming_hex(fa['ahash'],fb['ahash'])/64
    ss=float(ssim(ga,gb,data_range=255)); cs=contour_similarity(a,b)
    sift=cv2.SIFT_create(); k1,d1=sift.detectAndCompute(ga,None); k2,d2=sift.detectAndCompute(gb,None); good=[]; inl=0
    if d1 is not None and d2 is not None:
        for pair in cv2.BFMatcher().knnMatch(d1,d2,k=2):
            if len(pair)==2 and pair[0].distance < .70*pair[1].distance: good.append(pair[0])
        if len(good)>=4:
            src=np.float32([k1[m.queryIdx].pt for m in good]).reshape(-1,1,2); dst=np.float32([k2[m.trainIdx].pt for m in good]).reshape(-1,1,2)
            _,mask=cv2.findHomography(src,dst,cv2.RANSAC,4); inl=int(mask.sum()) if mask is not None else 0
    iok,iou,angle,mask_ssim=aligned_mask_identity(a,b)
    final=.15*max(0,ph)+.08*max(0,dh)+.07*max(0,ah)+.20*max(0,ss)+.20*cs+.30*min(1,inl/12)
    # EXACT_VISUAL_IDENTITY_100 requires independent agreement. It is intentionally strict.
    # For scanned/resized copies, pixel hashes can legitimately differ. A 100%
    # visual identity is allowed only when the normalized foreground geometry is
    # essentially identical and the independent structural checks agree.
    exact100=bool((exact and ph>=0.999 and dh>=0.999 and ah>=0.999 and ss>=0.997) or (iok and iou>=0.92 and mask_ssim>=0.995 and dh>=0.95 and ah>=0.85 and ss>=0.94 and inl>=10))
    return float(final),{'canonical_sha256':fa['sha256'],'mask_sha256':fa['mask_sha256'],'phash':round(float(ph),4),'dhash':round(float(dh),4),'ahash':round(float(ah),4),'ssim':round(ss,6),'contour_similarity':cs,'mask_iou':round(iou,6),'mask_ssim':round(mask_ssim,6),'mask_rotation':angle,'sift_good':len(good),'sift_inliers':inl},exact100


def visual_candidate_score(gov,client):
    ga,gb=canonical(gov),canonical(client)
    if ga is None or gb is None:return 0.0
    fa,fb=_image_hashes(ga),_image_hashes(gb)
    ph=1-_hamming_hex(fa[0],fb[0])/56; dh=1-_hamming_hex(fa[1],fb[1])/64; ah=1-_hamming_hex(fa[2],fb[2])/64
    return float(.55*ph+.30*dh+.15*ah)


def exact_sha_key(img):
    g=canonical(img); return sha_bytes(g.tobytes()) if g is not None else None


def pdf_vector_region(page,app_bbox):
    if not app_bbox:return None
    ax0,ay0,ax1,ay1=app_bbox; candidates=[]
    for d in page.get_drawings():
        r=d.get('rect')
        if not r or r.width<5 or r.height<5 or r.width*r.height>page.rect.width*page.rect.height*.70:continue
        dx=max(0,max(ax0-r.x1,r.x0-ax1)); dy=max(0,max(ay0-r.y1,r.y0-ay1)); dist=math.hypot(dx,dy)
        if dist<=max(page.rect.width,page.rect.height)*.40 and r.width*r.height>=20:candidates.append((dist,r,d))
    if not candidates:return None
    candidates.sort(key=lambda z:z[0]); base=candidates[0][0]; chosen=[x for x in candidates if x[0]<=base+max(35,page.rect.width*.08)][:60]
    x0=min(x[1].x0 for x in chosen); y0=min(x[1].y0 for x in chosen); x1=max(x[1].x1 for x in chosen); y1=max(x[1].y1 for x in chosen)
    if x1-x0<8 or y1-y0<8:return None
    return (x0,y0,x1,y1),'pdf_vector'


def find_mark_anchor(page,app_bbox):
    words=page_words(page); candidates=[]
    for w in words:
        t=norm_text(w[4])
        if t in MARK_WORDS or any(k in t for k in MARK_WORDS if len(k)>=5):
            x0,y0,x1,y1=w[:4]
            if app_bbox:
                ax0,ay0,ax1,ay1=app_bbox
                # prefer labels after application number and not far away
                dist=math.hypot(max(0,ax0-x1,x0-ax1),max(0,ay0-y1,y0-ay1))
                if y0 < ay0-20: dist += 150
            else: dist=0
            candidates.append((dist,(x0,y0,x1,y1),w[4]))
    if not candidates:
        try:
            toks=ocr_words(page)
            for t,x0,y0,x1,y1,*_ in toks:
                nt=norm_text(t)
                if nt in MARK_WORDS or any(k in nt for k in MARK_WORDS if len(k)>=5): candidates.append((0,(x0,y0,x1,y1),t))
        except Exception: pass
    candidates.sort(key=lambda x:x[0])
    return candidates[0][1] if candidates else None


def extract_logo_from_region(rendered,region,anchor=None,text_boxes=None):
    H,W=rendered.shape[:2]
    x0,y0,x1,y1=[max(0,int(v)) for v in region]; x1=min(W,x1); y1=min(H,y1)
    if x1<=x0 or y1<=y0:return None,None,None
    crop=rendered[y0:y1,x0:x1].copy(); gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
    mask=_foreground_mask(gray)
    # Suppress OCR text boxes inside the candidate area.
    if text_boxes:
        for bx0,by0,bx1,by1 in text_boxes:
            xx0=max(0,int(bx0-x0)-4); yy0=max(0,int(by0-y0)-4); xx1=min(crop.shape[1],int(bx1-x0)+4); yy1=min(crop.shape[0],int(by1-y0)+4)
            if xx1>xx0 and yy1>yy0: cv2.rectangle(mask,(xx0,yy0),(xx1,yy1),0,-1)
    # Group nearby disconnected strokes into one trademark object. Logos often
    # contain several separate shapes/letters that must be circled together.
    join=max(9,int(min(mask.shape)*0.035)); join=join if join%2==1 else join+1
    cluster_mask=cv2.dilate(mask,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(join,join)),iterations=1)
    n,labels,stats,cent=cv2.connectedComponentsWithStats(cluster_mask,8)
    candidates=[]; ch,cw=mask.shape
    for i in range(1,n):
        x,y,w,h,area=stats[i]
        if area<max(80,int(cw*ch*.0015)) or area>cw*ch*.55:continue
        if w<12 or h<12:continue
        aspect=w/max(h,1)
        if aspect>8 or aspect<.12:continue
        fill=area/max(w*h,1)
        # Candidate should look like a mark, not a single text line.
        if fill<.015:continue
        cx=x+w/2; cy=y+h/2
        if anchor:
            ax=(anchor[0]+anchor[2])/2-x0; ay=(anchor[1]+anchor[3])/2-y0
            dist=math.hypot((cx-ax)/max(cw,1),(cy-ay)/max(ch,1))
        else: dist=0.15
        size_score=min(1,(w*h)/(cw*ch*.20))
        score=1.2*size_score+0.7*min(fill/.25,1)-1.5*dist
        candidates.append((score,(x,y,x+w,y+h),area))
    if not candidates:return None,None,None
    candidates.sort(key=lambda z:z[0],reverse=True); bx=candidates[0][1]; pad=max(8,int(min(crop.shape[:2])*.015))
    bx=(max(0,bx[0]-pad),max(0,bx[1]-pad),min(cw,bx[2]+pad),min(ch,bx[3]+pad))
    full=(x0+bx[0],y0+bx[1],x0+bx[2],y0+bx[3])
    return rendered[full[1]:full[3],full[0]:full[2]].copy(),full,'scan_contour'


def tighten_box_by_contour(img,box):
    if img is None or box is None:return box,None
    x0,y0,x1,y1=[max(0,int(v)) for v in box]; x1=min(img.shape[1],x1); y1=min(img.shape[0],y1)
    crop=img[y0:y1,x0:x1]; sig=contour_signature(crop)
    if not sig:return box,None
    bx0,by0,bx1,by1=sig['bbox']; pad=max(4,int(min(x1-x0,y1-y0)*.02))
    tight=(max(0,x0+bx0-pad),max(0,y0+by0-pad),min(img.shape[1],x0+bx1+pad),min(img.shape[0],y0+by1+pad))
    return tight,sig


def government_mark_region(doc,page_no,app_bbox,rendered):
    page=doc[page_no-1]; H,W=rendered.shape[:2]; sx=W/page.rect.width; sy=H/page.rect.height
    mark_anchor=find_mark_anchor(page,app_bbox)
    # Native image objects first. Ignore full-page scan/background images.
    candidates=[]
    for info in page.get_images(full=True):
        xref=info[0]
        try: rects=page.get_image_rects(xref,transform=False)
        except Exception: rects=[]
        for r in rects:
            if r.width<8 or r.height<8 or r.width*r.height>page.rect.width*page.rect.height*.92:continue
            ref=mark_anchor or app_bbox
            if ref:
                ax0,ay0,ax1,ay1=ref; dx=max(0,max(ax0-r.x1,r.x0-ax1)); dy=max(0,max(ay0-r.y1,r.y0-ay1)); dist=math.hypot(dx,dy)
            else:dist=9999
            candidates.append((dist,r,xref))
    if candidates:
        candidates.sort(key=lambda x:x[0]); _,r,xref=candidates[0]
        try:
            data=doc.extract_image(xref); img=cv(data['image'])
            if img is not None:
                # Preserve the complete embedded image as the authoritative
                # government logo asset. A trademark may contain multiple
                # disconnected components, so never crop to only one contour
                # before matching. The native image rectangle is also the exact
                # PDF coordinate evidence boundary.
                return img,(int(r.x0*sx),int(r.y0*sy),int(r.x1*sx),int(r.y1*sy)),'pdf_image_native',{'pdf_rect':[r.x0,r.y0,r.x1,r.y1]}
        except Exception:pass
    # Vector geometry.
    vr=pdf_vector_region(page,app_bbox)
    if vr:
        r,kind=vr; box=tuple(int(v) for v in (r[0]*sx,r[1]*sy,r[2]*sx,r[3]*sy)); tight,_=tighten_box_by_contour(rendered,box)
        return rendered[tight[1]:tight[3],tight[0]:tight[2]].copy(),tight,kind,{'pdf_rect':[float(v) for v in r]}
    # Full-page scan / raster PDF: use label/app layout and then contour extraction.
    words=page_words(page)
    if not words:
        try:
            words=[(x0,y0,x1,y1,t) for t,x0,y0,x1,y1,*_ in ocr_words(page)]
        except Exception:
            words=[]
    text_boxes=[(w[0]*sx,w[1]*sy,w[2]*sx,w[3]*sy) for w in words]
    if mark_anchor:
        ax0,ay0,ax1,ay1=mark_anchor; rx0=max(0,int((ax0-80)*sx)); ry0=max(0,int((ay0-20)*sy)); rx1=min(W,int((ax1+500)*sx)); ry1=min(H,int((ay1+500)*sy))
        region=(rx0,ry0,rx1,ry1)
    elif app_bbox:
        x0,y0,x1,y1=app_bbox; region=(max(0,int((x0-80)*sx)),max(0,int((y0-30)*sy)),min(W,int((x1+520)*sx)),min(H,int((y1+600)*sy)))
    else: region=(0,0,W,H)
    logo,box,kind=extract_logo_from_region(rendered,region,mark_anchor and tuple(v*s for v in mark_anchor for s in []) if False else (tuple(v*sx for v in mark_anchor) if mark_anchor else None),text_boxes)
    if logo is not None:return logo,box,kind,{'pdf_rect':[box[0]/sx,box[1]/sy,box[2]/sx,box[3]/sy]}
    # Last fallback: broad region + contour tightening.
    tight,sig=tighten_box_by_contour(rendered,region)
    if tight:
        return rendered[tight[1]:tight[3],tight[0]:tight[2]].copy(),tight,'layout_contour',{'pdf_rect':[tight[0]/sx,tight[1]/sy,tight[2]/sx,tight[3]/sy]}
    return None,None,'failed',{}


def _ooxml_target(base_file,target):
    target=target.replace('\\','/'); base=Path(base_file).parent; p=(base/target).as_posix(); parts=[]
    for part in p.split('/'):
        if part=='..' and parts:parts.pop()
        elif part not in ('','.'):parts.append(part)
    p='/'.join(parts); return p if p.startswith('xl/') else 'xl/'+p.lstrip('/')


def extract_xlsx_media_row_map(xlsx):
    mapping={}
    with zipfile.ZipFile(xlsx,'r') as z:
        names=set(z.namelist())
        def rels_for(path):
            rp=str(Path(path).parent/'_rels'/(Path(path).name+'.rels')).replace('\\','/'); out={}
            if rp in names:
                rr=ET.fromstring(z.read(rp))
                for rel in rr:out[rel.attrib.get('Id')]=rel.attrib.get('Target','')
            return out
        wb_xml='xl/workbook.xml'; wb_rels='xl/_rels/workbook.xml.rels'
        if wb_xml not in names:return mapping
        ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'; nsr='http://schemas.openxmlformats.org/officeDocument/2006/relationships'; root=ET.fromstring(z.read(wb_xml)); wbrel={}
        if wb_rels in names:
            rr=ET.fromstring(z.read(wb_rels)); wbrel={rel.attrib.get('Id'):rel.attrib.get('Target','') for rel in rr}
        for sh in root.findall('{%s}sheets/{%s}sheet'%(ns,ns)):
            name=sh.attrib.get('name','Sheet'); rid=sh.attrib.get('{%s}id'%nsr); target=wbrel.get(rid)
            if not target:continue
            sp=_ooxml_target('xl/workbook.xml',target); srels=rels_for(sp)
            try:sr=ET.fromstring(z.read(sp))
            except Exception:continue
            de=sr.find('.//{%s}drawing'%nsr)
            if de is None:continue
            dp=_ooxml_target(sp,srels.get(de.attrib.get('{%s}id'%nsr),''));
            if dp not in names:continue
            drels=rels_for(dp)
            try:dr=ET.fromstring(z.read(dp))
            except Exception:continue
            nsx={'xdr':'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing','a':'http://schemas.openxmlformats.org/drawingml/2006/main'}
            for anc in dr:
                frm=anc.find('xdr:from',nsx); blip=anc.find('.//a:blip',nsx)
                if frm is None or blip is None:continue
                row=int(frm.findtext('xdr:row',default='0',namespaces=nsx))+1; col=int(frm.findtext('xdr:col',default='0',namespaces=nsx))+1
                rid2=blip.attrib.get('{%s}embed'%nsr); mp=_ooxml_target(dp,drels.get(rid2,''))
                if mp not in names:continue
                data=z.read(mp); ext=Path(mp).suffix.lower(); mapping.setdefault((name,row,col),[]).append({'data':data,'ext':ext,'source':mp,'drawing':dp})
    return mapping


def xlsx_client_records(xlsx,out):
    wb=load_workbook(xlsx,data_only=True)
    media=extract_xlsx_media_row_map(xlsx); outrec=[]
    for ws in wb.worksheets:
        header={}
        for r in range(1,min(30,ws.max_row)+1):
            for c in range(1,ws.max_column+1):
                v=str(ws.cell(r,c).value or '').lower().strip()
                if 'app' in v and ('number' in v or 'no' in v):header.setdefault('app',c)
                if any(k in v for k in ('company','applicant','owner','name')):header.setdefault('company',c)
                if any(k in v for k in ('mark','logo','device')):header.setdefault('mark',c)
            if 'app' in header:break
        ac=header.get('app',1); cc=header.get('company'); mc=header.get('mark')
        for r in range(1,ws.max_row+1):
            app=ws.cell(r,ac).value; n=norm_app(app)
            if not n:continue
            images=[]
            # Direct OOXML image mapping preserves WMF/EMF and exact anchor row/column.
            for (sheet_name,row_no,col_no),assets in media.items():
                if sheet_name==ws.title and row_no==r:
                    for a in assets:
                        im=cv(a['data'])
                        if im is None and a['ext'] in {'.wmf','.emf','.svg'}:im=render_vector_bytes(a['data'],a['ext'])
                        if im is not None:images.append({'img':im,'source':a['source'],'ext':a['ext'],'anchor_row':row_no,'anchor_col':col_no})
            # Openpyxl raster supplement. WMF warning is harmless because OOXML parser already handled it.
            for im in getattr(ws,'_images',[]):
                try:
                    rr=im.anchor._from.row+1
                    if rr==r:
                        raw=im._data(); obj=cv(raw)
                        if obj is not None:images.append({'img':obj,'source':'openpyxl','ext':'.raster','anchor_row':rr,'anchor_col':im.anchor._from.col+1})
                except Exception:pass
            mark=str(ws.cell(r,mc).value) if mc and ws.cell(r,mc).value is not None else ''
            outrec.append({'sheet':ws.title,'row':r,'application_raw':str(app),'application_no':n,'company':str(ws.cell(r,cc).value) if cc and ws.cell(r,cc).value is not None else '','mark':mark,'mark_norm':norm_text(mark),'images':images})
    return outrec


def circle_contour(img):
    if img is None:return None
    sig=contour_signature(img)
    if not sig:return None
    x0,y0,x1,y1=sig['bbox']; pad=max(8,int(min(img.shape[:2])*.08)); return (max(0,x0-pad),max(0,y0-pad),min(img.shape[1],x1+pad),min(img.shape[0],y1+pad))


def circle_box(img,box,label=None):
    if img is None or box is None:return img
    x0,y0,x1,y1=[int(v) for v in box]; cx=(x0+x1)//2; cy=(y0+y1)//2; rx=max(22,(x1-x0)//2+12); ry=max(22,(y1-y0)//2+12)
    out=img.copy(); cv2.ellipse(out,(cx,cy),(rx,ry),0,0,360,(0,0,255),7)
    if label:cv2.putText(out,label,(max(5,x0),max(25,y0-10)),cv2.FONT_HERSHEY_SIMPLEX,.62,(0,0,255),2)
    return out


def draw_evidence(page_img,mark_box,app_box,client_img,meta,out):
    left=page_img.copy()
    if mark_box:left=circle_box(left,mark_box,'GOVERNMENT LOGO')
    if app_box:
        x0,y0,x1,y1=[int(v) for v in app_box]; cv2.rectangle(left,(x0,y0),(x1,y1),(255,0,0),4); cv2.putText(left,meta['application_no'],(x0,max(22,y0-8)),cv2.FONT_HERSHEY_SIMPLEX,.55,(255,0,0),2)
    # Crop client logo to its actual contour before drawing the red circle.
    client_box=circle_contour(client_img); ci=client_img
    if client_box:
        ci=client_img[max(0,client_box[1]):client_box[3],max(0,client_box[0]):client_box[2]]
    rh=max(420,left.shape[0]); rw=max(420,min(700,max(360,left.shape[1]//2)))
    right=np.full((rh,rw,3),255,np.uint8)
    if ci is not None:
        scale=min((rw-40)/max(ci.shape[1],1),(rh-100)/max(ci.shape[0],1),1.0); cw=max(1,int(ci.shape[1]*scale)); ch=max(1,int(ci.shape[0]*scale)); ci=cv2.resize(ci,(cw,ch),interpolation=cv2.INTER_AREA)
        ox=(rw-cw)//2; oy=55; right[oy:oy+ch,ox:ox+cw]=ci; right=circle_box(right,(ox,oy,ox+cw,oy+ch),'CLIENT LOGO')
    cv2.putText(right,f"CLIENT: {meta['company']}",(15,28),cv2.FONT_HERSHEY_SIMPLEX,.55,(20,20,20),2)
    h=max(left.shape[0],right.shape[0]); w=left.shape[1]+right.shape[1]+30; canvas=np.full((h+80,w,3),255,np.uint8); canvas[60:60+left.shape[0],:left.shape[1]]=left; canvas[60:60+right.shape[0],left.shape[1]+30:]=right
    cv2.putText(canvas,f"GOV PAGE {meta['page']} | {meta['application_no']}",(10,35),cv2.FONT_HERSHEY_SIMPLEX,.7,(20,20,20),2)
    cv2.putText(canvas,f"{meta['decision']} | SCORE {meta['score']:.4f}",(left.shape[1]+35,35),cv2.FONT_HERSHEY_SIMPLEX,.55,(20,20,20),2)
    p=out/f"evidence_{meta['page']}_{meta['application_no'].replace('/','_')}_{meta['row']}.png"; cv2.imwrite(str(p),canvas); return p


def write_marked_pdf(doc,out,matches):
    # Add circles to the ORIGINAL government PDF at the stored PDF coordinates.
    for m in matches:
        if not m.get('mark_pdf_box'):continue
        p=doc[m['page']-1]; x0,y0,x1,y1=m['mark_pdf_box']; rect=pymupdf.Rect(x0,y0,x1,y1); cx=(rect.x0+rect.x1)/2; cy=(rect.y0+rect.y1)/2; rx=max(10,rect.width/2+5); ry=max(10,rect.height/2+5)
        shape=p.new_shape(); shape.draw_oval(pymupdf.Rect(cx-rx,cy-ry,cx+rx,cy+ry)); shape.finish(color=(1,0,0),width=2.5); shape.commit(); p.insert_text((rect.x0,max(10,rect.y0-5)),f"EXACT EVIDENCE | {m['application_no']} | {m['decision']}",fontsize=7,color=(1,0,0))
    doc.save(str(out/'government_marked_evidence.pdf'),garbage=4,deflate=True)


def make_xlsx(summary,matches,government_records,clients,out):
    wb=Workbook(); ws=wb.active; ws.title='Summary'
    for r,(k,v) in enumerate(summary.items(),1):ws.cell(r,1,k);ws.cell(r,2,v)
    ws2=wb.create_sheet('Matches'); headers=['page','application_no','client_application_no','application_no_exact_match','company','sheet','row','decision','score','exact_identity','match_origin','match_basis','government_pdf_bbox','government_render_bbox','client_media_source','contour_similarity','mask_iou','mask_ssim','sift_inliers','mark_pdf_box','evidence']
    for c,h in enumerate(headers,1):ws2.cell(1,c,h).font=Font(bold=True)
    for r,m in enumerate(matches,2):
        for c,h in enumerate(headers,1):ws2.cell(r,c,json.dumps(m.get(h,''),ensure_ascii=False) if isinstance(m.get(h,''),(list,dict)) else m.get(h,''))
    ws3=wb.create_sheet('Government Records'); gh=['page','application_no','application_raw','source','app_bbox','mark_bbox_pdf','mark_bbox_render','mark_extraction','logo_extracted']
    for c,h in enumerate(gh,1):ws3.cell(1,c,h).font=Font(bold=True)
    for r,g in enumerate(government_records,2):
        vals=[g.get('page'),g.get('application_no'),g.get('application_raw'),g.get('source'),g.get('app_bbox'),g.get('mark_bbox_pdf'),g.get('mark_bbox_render'),g.get('mark_extraction'),bool(g.get('logo_extracted'))]
        for c,v in enumerate(vals,1):ws3.cell(r,c,json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict,tuple)) else v)
    ws4=wb.create_sheet('Client Records'); ch=['sheet','row','application_no','company','mark','images_count','media_sources']
    for c,h in enumerate(ch,1):ws4.cell(1,c,h).font=Font(bold=True)
    for r,cx in enumerate(clients,2):
        vals=[cx['sheet'],cx['row'],cx['application_no'],cx['company'],cx['mark'],len(cx['images']),';'.join(str(i.get('source')) for i in cx['images'])]
        for c,v in enumerate(vals,1):ws4.cell(r,c,v)
    for sh in wb.worksheets:
        sh.freeze_panes='A2'; sh.auto_filter.ref=sh.dimensions
        for col in sh.columns:
            letter=col[0].column_letter; sh.column_dimensions[letter].width=min(70,max(12,max(len(str(x.value or '')) for x in col)+2))
    wb.save(out/'match_report.xlsx')


def _run_pdf_xlsx_analysis(jid,pdf,xlsx,d,jobs):
    try:
        work=d/'work';work.mkdir();report=d/'report';report.mkdir();evidence_dir=report/'evidence';evidence_dir.mkdir()
        doc=pymupdf.open(str(pdf));jobs[jid]={'status':'running','progress':1,'pdf_pages':len(doc)}
        prs=pdf_records(doc,work,lambda a,b,c:jobs[jid].update({'progress':int(a/max(b,1)*30),'pdf_pages':b,'government_records':c}))
        clients=xlsx_client_records(xlsx,work);jobs[jid].update({'progress':35,'client_records':len(clients)})
        by_app={}
        for c in clients:by_app.setdefault(c['application_no'],[]).append(c)
        assets=[]
        for c in clients:
            for a in c.get('images',[]):
                if a.get('img') is not None:assets.append((c,a))
        sha_index={};asset_fast=[]
        for c,a in assets:
            try:
                key=exact_sha_key(a['img'])
                if key:sha_index.setdefault(key,[]).append((c,a));asset_fast.append((c,a,key))
            except Exception:pass
        matches=[];app_hits=0;page_cache={};seen=set();logo_extracted=0
        for i,g in enumerate(prs):
            app_cands=by_app.get(g['application_no'],[])
            if app_cands:app_hits+=1
            if g['page'] not in page_cache:page_cache[g['page']]=render(doc,g['page']-1,work,220)
            page_img=cv(page_cache[g['page']]);gov_img,box,kind,geo=government_mark_region(doc,g['page'],g.get('app_bbox'),page_img)
            g['mark_bbox_render']=list(map(int,box)) if box else None;g['mark_extraction']=kind;g['mark_bbox_pdf']=geo.get('pdf_rect') if geo else None;g['logo_extracted']=gov_img is not None
            if gov_img is None:continue
            logo_extracted+=1
            gov_sha=exact_sha_key(gov_img); candidates=[]
            for c,a in sha_index.get(gov_sha,[]):candidates.append((1.0,c,a,'exact_sha'))
            if not candidates and assets:
                scored=[]
                for c,a,key in asset_fast:
                    if key==gov_sha:continue
                    try:
                        fs=visual_candidate_score(gov_img,a['img'])
                        if fs>=0.62:scored.append((fs,c,a,'hash_prefilter'))
                    except Exception:pass
                scored.sort(key=lambda x:x[0],reverse=True);candidates=scored[:20]
            # Application-number rows with logos are always reviewed, even if their logo hash is weak.
            existing={(c['sheet'],c['row'],a.get('source')) for _,c,a,_ in candidates}
            for c in app_cands:
                for a in c.get('images',[]):
                    k=(c['sheet'],c['row'],a.get('source'))
                    if a.get('img') is not None and k not in existing:candidates.append((visual_candidate_score(gov_img,a['img']),c,a,'application_match'))
            for pre,c,a,origin in candidates:
                pair=(g['page'],g['application_no'],c['sheet'],c['row'],a.get('source'))
                if pair in seen:continue
                seen.add(pair)
                sc,detail,exact=score(gov_img,a['img']);same_app=(c['application_no']==g['application_no'])
                if exact:decision='EXACT_VISUAL_IDENTITY_100'
                elif detail.get('sift_inliers',0)>=12 and detail.get('ssim',0)>=.985 and detail.get('phash',0)>=.98 and detail.get('contour_similarity',0)>=.95:decision='VERY_HIGH_VISUAL_IDENTITY_REVIEW_REQUIRED'
                elif sc>=.68:decision='HIGH_VISUAL_SIMILARITY_REVIEW_REQUIRED'
                else:continue
                page=doc[g['page']-1];sx=page_img.shape[1]/page.rect.width;sy=page_img.shape[0]/page.rect.height;pdf_box=[round(box[0]/sx,3),round(box[1]/sy,3),round(box[2]/sx,3),round(box[3]/sy,3)] if box else None
                m={'page':g['page'],'application_no':g['application_no'],'company':c['company'],'sheet':c['sheet'],'row':c['row'],'client_application_no':c['application_no'],'application_no_exact_match':same_app,'decision':decision,'score':round(sc,6),'exact_identity':exact,'match_origin':origin,'match_basis':kind,'government_pdf_bbox':pdf_box,'government_render_bbox':list(map(int,box)) if box else None,'client_media_source':a.get('source'),'contour_similarity':detail.get('contour_similarity'),'mask_iou':detail.get('mask_iou'),'mask_ssim':detail.get('mask_ssim'),'sift_inliers':detail.get('sift_inliers'),'mark_pdf_box':pdf_box,'evidence':''}
                ev=draw_evidence(page_img,box,g.get('app_bbox'),a['img'],m,evidence_dir);m['evidence']=str(Path('evidence')/ev.name) if ev else ''
                matches.append(m)
            jobs[jid]['progress']=35+int((i+1)/max(len(prs),1)*50)
        write_marked_pdf(doc,report,matches)
        summary={'pdf_pages':len(doc),'government_records':len(prs),'government_logos_extracted':logo_extracted,'client_records':len(clients),'client_logo_assets':len(assets),'application_matches':app_hits,'match_rows':len(matches),'exact_image_identity_100':sum(bool(m['exact_identity']) for m in matches),'very_high_visual_review':sum(m['decision']=='VERY_HIGH_VISUAL_IDENTITY_REVIEW_REQUIRED' for m in matches),'high_visual_review':sum(m['decision']=='HIGH_VISUAL_SIMILARITY_REVIEW_REQUIRED' for m in matches),'government_logo_extraction_failures':len(prs)-logo_extracted,'government_sha256':sha(pdf),'client_sha256':sha(xlsx),'engine':'AI Trademark Conflict Detector STRICT EXACT v2.4 — Global Government Logo vs Client Logo Matching + Contour/Coordinate Evidence + WMF/EMF','legal_status':'EXACT_VISUAL_IDENTITY_100 is deterministic digital/visual evidence, not a legal infringement or likelihood-of-confusion verdict.','evidence_policy':'Every government record is compared against client logo assets globally. Application No. equality is a separate evidence field and never blocks logo conflict detection. Exact evidence requires independent image/hash/mask/structural/contour agreement.'}
        (report/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8');(report/'matches.json').write_text(json.dumps(matches,ensure_ascii=False,indent=2),encoding='utf8')
        with open(report/'matches.csv','w',newline='',encoding='utf-8-sig') as f:
            fields=['page','application_no','client_application_no','application_no_exact_match','company','sheet','row','decision','score','exact_identity','match_origin','match_basis','government_pdf_bbox','government_render_bbox','client_media_source','contour_similarity','mask_iou','mask_ssim','sift_inliers','mark_pdf_box','evidence'];w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(matches)
        make_xlsx(summary,matches,prs,clients,report)
        jobs[jid]={**jobs[jid],'status':'completed','progress':100,**summary,'files':['match_report.xlsx','matches.csv','matches.json','summary.json','government_marked_evidence.pdf']}
    except Exception as e:
        jobs[jid]={'status':'error','progress':100,'error':str(e),'traceback':traceback.format_exc()}


def _source_records(source_type,path,work,progress):
    if source_type == 'pdf':
        doc=pymupdf.open(str(path))
        records=pdf_records(doc,work,progress)
        cache={}
        for index,record in enumerate(records):
            page_no=record['page']
            if page_no not in cache:
                cache[page_no]=cv(render(doc,page_no-1,work,220))
            page_img=cache[page_no]
            mark_img,box,method,geometry=government_mark_region(doc,page_no,record.get('app_bbox'),page_img)
            record.update({
                'record_id':index,
                'images':[{'img':mark_img,'source':method}] if mark_img is not None else [],
                'mark_bbox_render':list(map(int,box)) if box else None,
                'mark_bbox_pdf':geometry.get('pdf_rect') if geometry else None,
                'mark_extraction':method,
            })
        return records,doc
    if source_type in ('xlsx','xlsm'):
        records=xlsx_client_records(path,work)
        for index,record in enumerate(records):
            record['record_id']=index
        progress(len(records),max(len(records),1),len(records))
        return records,None
    raise ValueError(f"Unsupported source type: {source_type}")


def _source_location(record,source_type):
    if source_type == 'pdf':
        return {
            'page':record.get('page'),
            'sheet':'',
            'row':'',
            'company':record.get('company',''),
        }
    return {
        'page':'',
        'sheet':record.get('sheet',''),
        'row':record.get('row',''),
        'company':record.get('company',''),
    }


def _write_pair_evidence(left_img,right_img,left_label,right_label,out):
    def prepare(value):
        image=cv(value)
        if image is None:
            return Image.new('RGB',(480,320),'#e8ebf2')
        rgb=cv2.cvtColor(image,cv2.COLOR_BGR2RGB)
        result=Image.fromarray(rgb)
        result.thumbnail((480,320),Image.Resampling.LANCZOS)
        return result

    left=prepare(left_img)
    right=prepare(right_img)
    canvas=Image.new('RGB',(1000,370),'white')
    draw=ImageDraw.Draw(canvas)
    draw.text((12,10),left_label[:120],fill='#17213b')
    draw.text((508,10),right_label[:120],fill='#17213b')
    canvas.paste(left,(10,38))
    canvas.paste(right,(510,38))
    canvas.save(out,optimize=True)


def _write_comparison_xlsx(summary,matches,out):
    wb=Workbook()
    summary_ws=wb.active
    summary_ws.title='Summary'
    for row,(key,value) in enumerate(summary.items(),1):
        summary_ws.cell(row,1,key)
        summary_ws.cell(row,2,json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else value)
    match_ws=wb.create_sheet('Matches')
    fields=[
        'source_a_record_id','source_b_record_id','source_a_application_no','source_b_application_no','application_no_exact_match',
        'source_a_location','source_b_location','decision','score','exact_identity',
        'match_origin','match_basis','contour_similarity','mask_iou','mask_ssim',
        'sift_inliers','evidence',
    ]
    match_ws.append(fields)
    for match in matches:
        match_ws.append([match.get(field,'') for field in fields])
    for ws in (summary_ws,match_ws):
        for cell in ws[1]:
            cell.font=Font(bold=True)
        ws.freeze_panes='A2'
        ws.auto_filter.ref=ws.dimensions
    wb.save(out)


def _write_marked_source_pdf(doc,records,matches,side,out):
    record_ids={match[f'{side}_record_id'] for match in matches}
    for record in records:
        if record['record_id'] not in record_ids or not record.get('mark_bbox_pdf'):
            continue
        rect=pymupdf.Rect(*record['mark_bbox_pdf'])
        doc[record['page']-1].draw_rect(rect,color=(1,0.25,0.2),width=2,overlay=True)
    doc.save(str(out),garbage=4,deflate=True)


def _run_same_format_analysis(jid,source_a,source_b,d,jobs,source_type):
    docs=[]
    try:
        work=d/'work'
        work.mkdir()
        report=d/'report'
        report.mkdir()
        evidence_dir=report/'evidence'
        evidence_dir.mkdir()
        work_a=work/'source_a'
        work_b=work/'source_b'
        work_a.mkdir()
        work_b.mkdir()
        docs_format='pdf' if source_type == 'pdf' else 'xlsx'
        jobs[jid]={'status':'running','progress':1,'source_a_type':docs_format,'source_b_type':docs_format}
        records_a,doc_a=_source_records(docs_format,source_a,work_a,lambda current,total,count:jobs[jid].update({'progress':min(20,int(current/max(total,1)*20)),'source_a_records':count}))
        records_b,doc_b=_source_records(docs_format,source_b,work_b,lambda current,total,count:jobs[jid].update({'progress':20+min(20,int(current/max(total,1)*20)),'source_b_records':count}))
        if doc_a is not None:
            docs.append(doc_a)
        if doc_b is not None:
            docs.append(doc_b)
        assets_a=[(record,image) for record in records_a for image in record.get('images',[]) if image.get('img') is not None]
        assets_b=[(record,image) for record in records_b for image in record.get('images',[]) if image.get('img') is not None]
        sha_index={}
        assets_b_by_app={}
        for record,image in assets_b:
            assets_b_by_app.setdefault(record['application_no'],[]).append((record,image))
            image_key=exact_sha_key(image['img'])
            if image_key:
                sha_index.setdefault(image_key,[]).append((record,image))

        matches=[]
        seen=set()
        application_matches=sum(record['application_no'] in assets_b_by_app for record in records_a)
        for index,(record_a,image_a) in enumerate(assets_a):
            app_candidates=assets_b_by_app.get(record_a['application_no'],[])
            image_key=exact_sha_key(image_a['img'])
            candidates=[(record_b,image_b,1.0,'exact_sha') for record_b,image_b in sha_index.get(image_key,[])]
            if not candidates and assets_b:
                scored=[]
                for record_b,image_b in assets_b:
                    try:
                        fast_score=visual_candidate_score(image_a['img'],image_b['img'])
                        if fast_score>=0.62:
                            scored.append((fast_score,record_b,image_b,'hash_prefilter'))
                    except Exception:
                        continue
                scored.sort(key=lambda item:item[0],reverse=True)
                candidates.extend((record_b,image_b,fast_score,origin) for fast_score,record_b,image_b,origin in scored[:20])
            existing={(record_b['record_id'],image_b.get('source')) for record_b,image_b,_,_ in candidates}
            for record_b,image_b in app_candidates:
                key=(record_b['record_id'],image_b.get('source'))
                if key not in existing:
                    candidates.append((record_b,image_b,visual_candidate_score(image_a['img'],image_b['img']), 'application_match'))
                    existing.add(key)

            for record_b,image_b,_,origin in candidates:
                pair=(record_a['record_id'],record_b['record_id'],image_a.get('source'),image_b.get('source'))
                if pair in seen:
                    continue
                seen.add(pair)
                similarity,detail,exact=score(image_a['img'],image_b['img'])
                if exact:
                    decision='EXACT_VISUAL_IDENTITY_100'
                elif detail.get('sift_inliers',0)>=12 and detail.get('ssim',0)>=.985 and detail.get('phash',0)>=.98 and detail.get('contour_similarity',0)>=.95:
                    decision='VERY_HIGH_VISUAL_IDENTITY_REVIEW_REQUIRED'
                elif similarity>=.68:
                    decision='HIGH_VISUAL_SIMILARITY_REVIEW_REQUIRED'
                else:
                    continue
                location_a=_source_location(record_a,docs_format)
                location_b=_source_location(record_b,docs_format)
                evidence_name=f"pair_{len(matches)+1:05d}.png"
                evidence_path=evidence_dir/evidence_name
                label_a=f"Source A | {record_a['application_no']} | {location_a}"
                label_b=f"Source B | {record_b['application_no']} | {location_b}"
                _write_pair_evidence(image_a['img'],image_b['img'],label_a,label_b,evidence_path)
                matches.append({
                    'source_a_record_id':record_a['record_id'],
                    'source_b_record_id':record_b['record_id'],
                    'source_a_application_no':record_a['application_no'],
                    'source_b_application_no':record_b['application_no'],
                    'application_no_exact_match':record_a['application_no']==record_b['application_no'],
                    'source_a_location':json.dumps(location_a,ensure_ascii=False),
                    'source_b_location':json.dumps(location_b,ensure_ascii=False),
                    'decision':decision,
                    'score':round(float(similarity),6),
                    'exact_identity':bool(exact),
                    'match_origin':origin,
                    'match_basis':f"{image_a.get('source','')} / {image_b.get('source','')}",
                    'contour_similarity':detail.get('contour_similarity'),
                    'mask_iou':detail.get('mask_iou'),
                    'mask_ssim':detail.get('mask_ssim'),
                    'sift_inliers':detail.get('sift_inliers'),
                    'evidence':str(Path('evidence')/evidence_name),
                })
            jobs[jid]['progress']=40+int((index+1)/max(len(assets_a),1)*40)

        summary={
            'source_a_type':docs_format,
            'source_b_type':docs_format,
            'source_a_records':len(records_a),
            'source_b_records':len(records_b),
            'source_a_logo_assets':len(assets_a),
            'source_b_logo_assets':len(assets_b),
            'application_matches':application_matches,
            'match_rows':len(matches),
            'exact_image_identity_100':sum(bool(match['exact_identity']) for match in matches),
            'very_high_visual_review':sum(match['decision']=='VERY_HIGH_VISUAL_IDENTITY_REVIEW_REQUIRED' for match in matches),
            'high_visual_review':sum(match['decision']=='HIGH_VISUAL_SIMILARITY_REVIEW_REQUIRED' for match in matches),
            'source_a_sha256':sha(source_a),
            'source_b_sha256':sha(source_b),
            'engine':'STRICT EXACT v2.4 — Global visual matching + SHA-256/pHash/dHash + SSIM/contour/SIFT evidence',
            'evidence_policy':'Visual identity and application-number equality are separate evidence fields. Similarity findings require human/legal review.',
        }
        files=[]
        if docs_format=='pdf':
            marked_a=report/'source_a_marked_evidence.pdf'
            marked_b=report/'source_b_marked_evidence.pdf'
            _write_marked_source_pdf(doc_a,records_a,matches,'source_a',marked_a)
            _write_marked_source_pdf(doc_b,records_b,matches,'source_b',marked_b)
            files.extend([marked_a.name,marked_b.name])
        (report/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
        (report/'matches.json').write_text(json.dumps(matches,ensure_ascii=False,indent=2),encoding='utf8')
        fields=[
            'source_a_record_id','source_b_record_id','source_a_application_no','source_b_application_no','application_no_exact_match',
            'source_a_location','source_b_location','decision','score','exact_identity',
            'match_origin','match_basis','contour_similarity','mask_iou','mask_ssim','sift_inliers','evidence',
        ]
        with open(report/'matches.csv','w',newline='',encoding='utf-8-sig') as output:
            writer=csv.DictWriter(output,fieldnames=fields)
            writer.writeheader()
            writer.writerows(matches)
        _write_comparison_xlsx(summary,matches,report/'match_report.xlsx')
        with zipfile.ZipFile(report/'visual_evidence.zip','w',compression=zipfile.ZIP_DEFLATED) as archive:
            for evidence_file in sorted(evidence_dir.glob('*.png')):
                archive.write(evidence_file,Path('evidence')/evidence_file.name)
        files.extend(['match_report.xlsx','matches.csv','matches.json','summary.json','visual_evidence.zip'])
        jobs[jid]={
            **jobs[jid],
            **summary,
            'status':'completed',
            'progress':100,
            'files':files,
        }
    except Exception as error:
        jobs[jid]={'status':'error','progress':100,'error':str(error),'traceback':traceback.format_exc()}
    finally:
        for doc in docs:
            doc.close()


def run_analysis(jid,source_a,source_b,d,jobs,source_a_type='pdf',source_b_type='xlsx'):
    format_a='pdf' if source_a_type=='pdf' else 'xlsx'
    format_b='pdf' if source_b_type=='pdf' else 'xlsx'
    if format_a==format_b:
        _run_same_format_analysis(jid,source_a,source_b,d,jobs,format_a)
        return
    if format_a=='pdf':
        _run_pdf_xlsx_analysis(jid,source_a,source_b,d,jobs)
        jobs[jid].update({
            'source_a_type':format_a,
            'source_b_type':format_b,
            'source_a_records':jobs[jid].get('government_records',0),
            'source_b_records':jobs[jid].get('client_records',0),
            'source_a_logo_assets':jobs[jid].get('government_logos_extracted',0),
            'source_b_logo_assets':jobs[jid].get('client_logo_assets',0),
        })
    else:
        _run_pdf_xlsx_analysis(jid,source_b,source_a,d,jobs)
        jobs[jid].update({
            'source_a_type':format_a,
            'source_b_type':format_b,
            'source_a_records':jobs[jid].get('client_records',0),
            'source_b_records':jobs[jid].get('government_records',0),
            'source_a_logo_assets':jobs[jid].get('client_logo_assets',0),
            'source_b_logo_assets':jobs[jid].get('government_logos_extracted',0),
        })
