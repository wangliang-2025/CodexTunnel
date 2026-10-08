"""Generate the vector-native SSH tunnel symbol and Windows icon sizes."""
from pathlib import Path
from PIL import Image, ImageDraw
ROOT=Path(__file__).resolve().parent

def generate_app_icon(output_path='app_icon.ico'):
    assets=ROOT/'assets'; assets.mkdir(exist_ok=True)
    paths=['M80 76H60V180H80','M176 76H196V180H176','M94 108H162L148 94','M162 108L148 122','M162 148H94L108 134','M94 148L108 162']
    svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><rect x="8" y="8" width="240" height="240" rx="54" fill="#DB648E"/><g fill="none" stroke="#FFFFFF" stroke-width="14" stroke-linecap="round" stroke-linejoin="round">'+''.join(f'<path d="{p}"/>' for p in paths)+'</g></svg>'
    (assets/'tunnel-icon.svg').write_text(svg,encoding='utf-8')
    scale=4
    image=Image.new('RGBA',(256*scale,256*scale),(0,0,0,0)); draw=ImageDraw.Draw(image)
    draw.rounded_rectangle(tuple(v*scale for v in (8,8,248,248)),radius=54*scale,fill='#DB648E')
    lines=[[(80,76),(60,76),(60,180),(80,180)],[(176,76),(196,76),(196,180),(176,180)],[(94,108),(162,108),(148,94)],[(162,108),(148,122)],[(162,148),(94,148),(108,134)],[(94,148),(108,162)]]
    for points in lines:
        draw.line([(x*scale,y*scale) for x,y in points],fill='white',width=14*scale,joint='curve')
        for x,y in points: draw.ellipse(((x-7)*scale,(y-7)*scale,(x+7)*scale,(y+7)*scale),fill='white')
    image=image.resize((256,256),Image.Resampling.LANCZOS)
    image.save(assets/'tunnel-icon.png')
    image.save(ROOT/output_path,format='ICO',sizes=[(n,n) for n in (16,24,32,48,64,128,256)])
    print(f'Icon exported: {ROOT/output_path}')

if __name__=='__main__': generate_app_icon()
