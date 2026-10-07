"""Base read reservations, demand-guarded survival loans and an elastic pool.

New counters share the original total window expiry. Unclassified legacy reads
consume ordinary reservations conservatively, never reset the total or its TTL.
Only critical reads borrow idle survival capacity; a ledger preserves spent loans.
"""
from __future__ import annotations

LANE_INDEX = {"ad": 0, "survival": 1, "critical": 2, "sync": 3, "routine": 4, "background": 4}
def listener_reserve(total: int) -> int:
    """Bounded control traffic, carved out of the existing survival reservation."""
    return max(3, (total * 2 + 99) // 100)


COUNTERS = ("ad", "survival", "join_reserved", "sync_reserved", "flex_reserved")


def allocations(total: int) -> list[int]:
    protected = [(total * share + 99) // 100 for share in (35, 15, 25)]
    sync = total * 20 // 100
    return [*protected, sync, total - sum(protected) - sync]


def classified(total: int, measured: list[int], caps: list[int]) -> list[int]:
    remaining = total
    used = []
    for value in measured:
        value = min(remaining, max(0, value))
        used.append(value)
        remaining -= value
    # The old ordinary pool had no aligned join/sync breakdown. Charge its
    # historical spend to sync, then joining, then elastic capacity.
    for index in (3, 2):
        charge = min(remaining, max(0, caps[index] - used[index]))
        used[index] += charge
        remaining -= charge
    used[4] += remaining
    return used


def available(total: int, used: list[int], caps: list[int], lane: str) -> int:
    index = LANE_INDEX[lane]
    remaining = max(0, total - sum(used))
    protected_other = 0
    for i in (1, 0, 2, 3):
        claim = min(remaining, max(0, caps[i] - used[i]))
        remaining -= claim
        if i != index:
            protected_other += claim
    return max(0, total - sum(used) - protected_other)


def lending_caps(caps: list[int], used: list[int], lane: str, hold: int = -1, sync_hold: int = -1, ad_hold: int = -1) -> list[int]:
    """Keep the survival work buffer; only critical work receives the excess."""
    adjusted = list(caps)
    if lane == "critical" and hold >= 0:
        loan = max(0, caps[1] - used[1] - hold)
        adjusted[1] -= loan
        adjusted[2] += loan
    if lane == "critical" and sync_hold >= 0:
        loan = max(0, caps[3] - used[3] - sync_hold)
        adjusted[3] -= loan
        adjusted[2] += loan
    if lane == "critical" and ad_hold >= 0:
        loan = max(0, caps[0] - used[0] - ad_hold)
        adjusted[0] -= loan
        adjusted[2] += loan
    return adjusted


def transferred_caps(caps: list[int], lent: int, sync_lent: int = 0, ad_lent: int = 0) -> list[int]:
    """Spent loans stay transferred until the original window expires."""
    adjusted = list(caps)
    lent = min(caps[1], max(0, lent))
    adjusted[1] -= lent
    adjusted[2] += lent
    sync_lent = min(caps[3], max(0, sync_lent))
    adjusted[3] -= sync_lent
    adjusted[2] += sync_lent
    ad_lent = min(caps[0], max(0, ad_lent))
    adjusted[0] -= ad_lent
    adjusted[2] += ad_lent
    return adjusted


def ad_send_hold(ad_allocation: int, flex_allocation: int) -> int:
    """Keep 20% for sending, 20% planning buffer, and flex for delivery.

    Ad pacing plans against 80% of the base reservation. Letting refreshes
    spend that entire amount would strand sends despite nominal read headroom.
    """
    return flex_allocation + ad_allocation - ad_allocation * 3 // 5


def reservation_args(account_id: int, limits: dict, lane: str, reads: int, pace: int, *, workload: str | None = None, ad_refresh: bool = False,
                     work_token: str | None = None, work_mode: str = "read", work_group: int = 0,
                     work_scope: str = "", work_kind: str = "", sync_completion: bool = False,
                     sync_catchup: bool = False) -> tuple[list[str], list[int | str]]:
    names = ["minute", "hour", "day"] + [f"{name}_{window}" for name in COUNTERS for window in ("hour", "day")]
    keys = [f"vanguard:rpc:{account_id}:{name}" for name in names]
    # A progressing server page must not inherit several minutes of idle-poll
    # smoothing. Both clocks share all quotas below; the normal clock remains
    # intact when catch-up completes, and catch-up readers share their burst.
    pace_key = ("sync_catchup_pace" if sync_catchup else "sync_pace") if lane == "sync" else "scan_pace"
    keys.append(f"vanguard:rpc:{account_id}:{pace_key}")
    keys.extend(f"vanguard:rpc:{account_id}:survival_lent_{window}" for window in ("hour", "day"))
    keys.extend(f"vanguard:rpc:{account_id}:listener_{window}" for window in ("hour", "day"))
    keys.extend(f"vanguard:rpc:{account_id}:critical_{kind}_{window}" for kind in ("join", "review") for window in ("hour", "day"))
    from app.core.account.read_work import LEASE_SECONDS, current, key
    active = current(account_id)
    if work_token is None:
        work_token = active.token if active is not None and active.claimed and (
            lane == ('ad' if active.kind == 'renewal' else 'critical')
        ) else ""
    keys.append(key(account_id))
    keys.extend(f"vanguard:rpc:{account_id}:sync_lent_{window}" for window in ("hour", "day"))
    keys.extend(f"vanguard:rpc:{account_id}:ad_lent_{window}" for window in ("hour", "day"))
    keys.extend(f"vanguard:rpc:{account_id}:sync_completion_{window}" for window in ("hour", "day"))
    args = [reads, pace, 4 if lane == "sync" else 1, (2 if lane == "listener" else LANE_INDEX[lane] + 1),
            limits["minute"], limits["hour"], limits["day"],
            *allocations(limits["hour"]), *allocations(limits["day"]),
            limits.get("survival_hold_hour", -1), limits.get("survival_hold_day", -1),
            int(lane == "listener"), listener_reserve(limits["hour"]), listener_reserve(limits["day"]),
            (1 if workload == "join" else 2) if lane == "critical" and workload else 0,
            int(ad_refresh), work_token, work_mode, LEASE_SECONDS * 1000,
            work_group, work_scope, work_kind,
            limits.get("sync_hold_hour", -1), limits.get("sync_hold_day", -1),
            limits.get('join_idle_hold_hour', -1), limits.get('join_idle_hold_day', -1),
            limits.get('ad_refresh_hold_hour', -1), limits.get('ad_refresh_hold_day', -1),
            limits.get('ad_demand_hold_hour', -1), limits.get('ad_demand_hold_day', -1),
            int(sync_completion), min(24, allocations(limits['hour'])[1] // 2),
            min(96, allocations(limits['day'])[1] // 3)]
    return keys, args


RESERVED_BUDGET_LUA = """
local cost,pace,burst,lane=tonumber(ARGV[1]),tonumber(ARGV[2]),tonumber(ARGV[3]),tonumber(ARGV[4])
local durations={60,3600,86400}
local delay=0
local loans={0,0}
local sync_loans={0,0}
local ad_loans={0,0}
local mode=ARGV[26] or 'read'
local completing=tonumber(ARGV[39] or '0')==1
if completing and (lane~=2 or mode~='read' or tonumber(ARGV[20])==1) then return 60000 end
local token=ARGV[25] or ''
local held_token=redis.call('HGET',KEYS[23],'token')
local held=tonumber(redis.call('HGET',KEYS[23],'remaining') or '0')
local held_kind=redis.call('HGET',KEYS[23],'kind')
local held_lane=tonumber(redis.call('HGET',KEYS[23],'lane') or '3')
local owns=token~='' and held_token==token
if mode=='reserve' and held_token then
 if held_token=='pending' and ARGV[30]=='review'
   and redis.call('HGET',KEYS[23],'group')==ARGV[28]
   and redis.call('HGET',KEYS[23],'scope')==ARGV[29] then
  redis.call('HSET',KEYS[23],'token',token)
  return 0
 end
 return math.max(1000,redis.call('PTTL',KEYS[23]))
end
if mode=='read' and token~='' and (not owns or cost>held) then return 1000 end
local function ttl(i)
 local value=redis.call('TTL',KEYS[i]); if value<1 then value=durations[i] end; return value
end
for i=1,3 do
 local used=tonumber(redis.call('GET',KEYS[i]) or '0')
 local protected_work=0;if not owns then protected_work=held end
 if used+cost+protected_work>tonumber(ARGV[4+i]) then delay=math.max(delay,ttl(i)) end
end
for w=1,2 do
 local total=tonumber(redis.call('GET',KEYS[w+1]) or '0')
 local left=total; local used={};local cap={}
 for i=1,5 do
  cap[i]=tonumber(ARGV[7+(w-1)*5+i])
  used[i]=math.min(left,math.max(0,tonumber(redis.call('GET',KEYS[3+(i-1)*2+w]) or '0')))
  left=left-used[i]
 end
 for _,i in ipairs({4,3}) do
  local charge=math.min(left,math.max(0,cap[i]-used[i]));used[i]=used[i]+charge;left=left-charge
 end
 used[5]=used[5]+left
 local lent=math.min(cap[2],math.max(0,tonumber(redis.call('GET',KEYS[14+w]) or '0')))
 if redis.call('EXISTS',KEYS[w+1])==0 then lent=0 end
 cap[2]=cap[2]-lent;cap[3]=cap[3]+lent
 local sync_lent=math.min(cap[4],math.max(0,tonumber(redis.call('GET',KEYS[23+w]) or '0')))
 if redis.call('EXISTS',KEYS[w+1])==0 then sync_lent=0 end
 cap[4]=cap[4]-sync_lent;cap[3]=cap[3]+sync_lent
 local ad_lent=math.min(cap[1],math.max(0,tonumber(redis.call('GET',KEYS[25+w]) or '0')))
 if redis.call('EXISTS',KEYS[w+1])==0 then ad_lent=0 end
 cap[1]=cap[1]-ad_lent;cap[3]=cap[3]+ad_lent
 local pending_survival=tonumber(redis.call('HGET',KEYS[23],'survival_'..w) or '0')
 local pending_sync=tonumber(redis.call('HGET',KEYS[23],'sync_'..w) or '0')
 local pending_ad=tonumber(redis.call('HGET',KEYS[23],'ad_'..w) or '0')
 if not owns then
  cap[2]=cap[2]-pending_survival;cap[4]=cap[4]-pending_sync
  cap[1]=cap[1]-pending_ad
  cap[3]=cap[3]+pending_survival+pending_sync+pending_ad
 end
 local function protected()
  local reserved=0; local remaining=math.max(0,tonumber(ARGV[5+w])-total)
  for _,i in ipairs({2,1,3,4}) do
   local demand=math.max(0,cap[i]-used[i])
   if i==held_lane and not owns then demand=math.max(demand,held) end
   local claim=math.min(remaining,demand);remaining=remaining-claim
   if i~=lane then reserved=reserved+claim end
  end
  return reserved
 end
 local recovery_used=tonumber(redis.call('GET',KEYS[16+w]) or '0')
 if redis.call('EXISTS',KEYS[w+1])==0 then recovery_used=0 end
 local recovery=math.max(0,tonumber(ARGV[20+w])-recovery_used)
 local listener=tonumber(ARGV[20])==1
 if listener and cost>recovery then delay=math.max(delay,ttl(w+1)) end
 local hold=tonumber(ARGV[17+w] or '-1')
 if hold>=0 then hold=math.max(hold,recovery) end
 local critical_hold=0;if lane==3 and held_lane==3 and not owns then critical_hold=held end
 local group=tonumber(ARGV[23] or '0')
 if lane==3 and group>0 and not owns then
  local joins=0;local reviews=0
  if redis.call('EXISTS',KEYS[w+1])==1 then
   joins=math.min(used[3],math.max(0,tonumber(redis.call('GET',KEYS[18+w]) or '0')))
   reviews=math.min(used[3]-joins,math.max(0,tonumber(redis.call('GET',KEYS[20+w]) or '0')))
  end
  local legacy=math.max(0,used[3]-joins-reviews)
  joins=joins+math.floor(legacy/2);reviews=reviews+legacy-math.floor(legacy/2)
  local other=joins;if group==1 then other=reviews end
  local other_min=math.max(0,math.floor(tonumber(ARGV[10+(w-1)*5])/4)-other)
  local idle_hold=tonumber(ARGV[32+w] or '-1')
  if group==2 and idle_hold>=0 then other_min=math.min(other_min,idle_hold) end
  local opposite=(group==1 and held_kind=='review') or (group==2 and held_kind=='join')
  if opposite and not owns then critical_hold=math.max(critical_hold,other_min)
  else critical_hold=critical_hold+other_min end
 end
 if lane==3 then
  -- Earlier lanes may currently be underfunded by legacy/other-lane spend.
  -- protected() clips those claims to the remaining total, so subtracting
  -- room once underestimates the loan: lending also restores clipped claims.
  -- Find the smallest loan that actually admits the operation, or exhaust
  -- this donor before trying the next one. No spent counter changes here.
  local function borrow(donor,allowed)
   local original,critical=cap[donor],cap[3]
   local low,high=0,math.max(0,allowed)
   while low<high do
    local middle=math.floor((low+high)/2)
    cap[donor]=original-middle;cap[3]=critical+middle
    local room=tonumber(ARGV[5+w])-total-protected()-critical_hold
    if room>=cost then high=middle else low=middle+1 end
   end
   cap[donor]=original-low;cap[3]=critical+low
   return low
  end
  local allowed=0
  if owns then allowed=pending_survival
  elseif hold>=0 then allowed=math.max(0,cap[2]-used[2]-hold) end
  loans[w]=borrow(2,allowed)
  local sync_hold=tonumber(ARGV[30+w] or '-1')
  allowed=0
  if owns then allowed=pending_sync
  elseif sync_hold>=0 then allowed=math.max(0,cap[4]-used[4]-sync_hold) end
  sync_loans[w]=borrow(4,allowed)
  local ad_hold=tonumber(ARGV[36+w] or '-1')
  allowed=0
  if owns then allowed=pending_ad
  elseif ad_hold>=0 then allowed=math.max(0,cap[1]-used[1]-ad_hold) end
  ad_loans[w]=borrow(1,allowed)
 end
 local reserved=protected()+critical_hold
 if lane~=3 and lane==held_lane and not owns then reserved=reserved+held end
 if lane==2 and not listener then reserved=reserved+recovery end
 if completing then
  local completed=tonumber(redis.call('GET',KEYS[27+w]) or '0')
  if redis.call('EXISTS',KEYS[w+1])==0 then completed=0 end
  -- Idle capacity only: retain real survival demand and all control headroom.
  if hold<0 then return 60000 end
  if cost>math.max(0,cap[2]-used[2]-hold) then delay=math.max(delay,ttl(w+1)) end
  if completed+cost>tonumber(ARGV[39+w]) then delay=math.max(delay,ttl(w+1)) end
 end
 -- The owner passed this admission hold when its slice was reserved. Other
 -- ad sends may use their cushion while the owner's remaining slice stays held.
 if lane==1 and tonumber(ARGV[24] or '0')==1 and not owns then
  local ad=tonumber(ARGV[8+(w-1)*5]);local flex=tonumber(ARGV[12+(w-1)*5])
  local refresh_hold=tonumber(ARGV[34+w] or '-1')
  if refresh_hold<0 then refresh_hold=flex+ad-math.floor(ad*3/5) end
  reserved=reserved+refresh_hold
 end
 if total+cost+reserved>tonumber(ARGV[5+w]) then delay=math.max(delay,ttl(w+1)) end
end
if delay>0 then return delay*1000 end
if mode=='reserve' then
 local expires=tonumber(ARGV[27])
 for i=2,3 do
  local remain=redis.call('PTTL',KEYS[i]);if remain>0 then expires=math.min(expires,remain) end
 end
 redis.call('HSET',KEYS[23],'token',token,'remaining',cost,'group',ARGV[28],
   'scope',ARGV[29],'kind',ARGV[30], 'lane',lane, 'survival_1',loans[1], 'survival_2',loans[2],
   'sync_1',sync_loans[1], 'sync_2',sync_loans[2], 'ad_1',ad_loans[1], 'ad_2',ad_loans[2])
 redis.call('PEXPIRE',KEYS[23],expires)
 return 0
end
local t=redis.call('TIME');local now=tonumber(t[1])*1000+math.floor(tonumber(t[2])/1000)
if pace>0 then
 local due=tonumber(redis.call('GET',KEYS[14]) or '0')
 local eligible=due-math.max(0,burst-cost)*pace
 if eligible>now then return -(eligible-now) end
end
for i=1,3 do
 local count=redis.call('INCRBY',KEYS[i],cost)
 if count==cost then
  redis.call('EXPIRE',KEYS[i],durations[i])
  if i>1 then
   for j=1,5 do redis.call('DEL',KEYS[3+(j-1)*2+i-1]) end
   redis.call('DEL',KEYS[13+i])
   redis.call('DEL',KEYS[15+i])
   redis.call('DEL',KEYS[17+i],KEYS[19+i])
   redis.call('DEL',KEYS[22+i])
   redis.call('DEL',KEYS[24+i])
   redis.call('DEL',KEYS[26+i])
  end
 end
end
for w=1,2 do
 local key=KEYS[3+(lane-1)*2+w]
 redis.call('INCRBY',key,cost)
 local expiry=redis.call('PTTL',KEYS[w+1]); if expiry>0 then redis.call('PEXPIRE',key,expiry) end
 if tonumber(ARGV[20])==1 then
  redis.call('INCRBY',KEYS[16+w],cost)
  if expiry>0 then redis.call('PEXPIRE',KEYS[16+w],expiry) end
 end
 if completing then
  redis.call('INCRBY',KEYS[27+w],cost)
  if expiry>0 then redis.call('PEXPIRE',KEYS[27+w],expiry) end
 end
 if loans[w]>0 then
  redis.call('INCRBY',KEYS[14+w],loans[w])
  if expiry>0 then redis.call('PEXPIRE',KEYS[14+w],expiry) end
 end
 if sync_loans[w]>0 then
  redis.call('INCRBY',KEYS[23+w],sync_loans[w])
  if expiry>0 then redis.call('PEXPIRE',KEYS[23+w],expiry) end
 end
 if ad_loans[w]>0 then
  redis.call('INCRBY',KEYS[25+w],ad_loans[w])
  if expiry>0 then redis.call('PEXPIRE',KEYS[25+w],expiry) end
 end
 if owns then
  if loans[w]>0 then redis.call('HINCRBY',KEYS[23],'survival_'..w,-loans[w]) end
  if sync_loans[w]>0 then redis.call('HINCRBY',KEYS[23],'sync_'..w,-sync_loans[w]) end
  if ad_loans[w]>0 then redis.call('HINCRBY',KEYS[23],'ad_'..w,-ad_loans[w]) end
 end
 local group=tonumber(ARGV[23] or '0')
 if lane==3 and group>0 then
  local groupkey=KEYS[18+(group-1)*2+w]
  redis.call('INCRBY',groupkey,cost)
  if expiry>0 then redis.call('PEXPIRE',groupkey,expiry) end
 end
end
if pace>0 then
 local due=math.max(now,tonumber(redis.call('GET',KEYS[14]) or '0'))+pace*cost
 redis.call('SET',KEYS[14],due,'PX',due-now+60000)
end
if owns then redis.call('HINCRBY',KEYS[23],'remaining',-cost) end
return 0
"""
