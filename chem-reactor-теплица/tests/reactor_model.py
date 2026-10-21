from dataclasses import replace
class ReactorModel:
    def __init__(self):
        self.rate=[0,0,0]; self.micro=[4,4,4]; self.enabled=[False]*3
        self.u=[0]*3; self.i=[0]*3
        self.minutes=[0]*3
    def packet(self,r):
        c,idx=r.command,r.idx; d=bytes(8); status=0
        value=int.from_bytes(r.data[:4],"big")
        if c==0x6c: self.minutes[idx]=value
        elif c==0x6b:
            minutes=self.minutes[idx]
            d=(minutes*60).to_bytes(4,"big")+minutes.to_bytes(2,"big")+bytes([bool(minutes),0])
        elif c==0x60:
            d=(2512).to_bytes(4,"big")+bytes.fromhex("03 00 00 00")
        elif c in (0x61,0x62):
            rate=int.from_bytes(r.data[:4],"big",signed=True)
            if c==0x62: rate=round(rate*200*self.micro[idx]/60)
            if abs(rate)>7000000: status=2
            else: self.rate[idx]=rate; self.enabled[idx]=rate!=0
        elif c==0x63: self.rate[idx]=0; self.enabled[idx]=False
        elif c==0x6a: self.rate=[0]*3; self.enabled=[False]*3
        elif c==0x64:
            if self.rate[idx]: status=6
            else: self.enabled[idx]=bool(r.data[0])
        elif c==0x65:
            if self.enabled[idx]: status=6
            else: self.micro[idx]=r.data[0]
        elif c==0x66:
            d=self.rate[idx].to_bytes(4,"big",signed=True)*2
        elif c==0x67:
            d=bytes([self.enabled[idx],bool(self.rate[idx]),0,self.micro[idx]])+bytes.fromhex("00 c8 00 00")
        elif c==0x68: pass
        elif c in (0x70,0x71,0x72,0x73,0x74,0x75):
            if c==0x70: self.u[idx]=value
            if c==0x71: self.i[idx]=value
            result=({0x70:self.u[idx],0x71:self.i[idx],0x72:-1000,0x73:12345,
                     0x74:self.u[idx],0x75:self.i[idx]})[c]
            d=(result&0xffffffff).to_bytes(4,"big")+bytes(4)
        else: status=3
        return replace(r,kind=2,status=status,data=bytes(8) if status else d)
