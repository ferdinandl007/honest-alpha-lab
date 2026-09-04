'use client';
import { useEffect, useRef, useState } from 'react';
import { Activity, ArrowUpRight, LockKeyhole, Pause, ShieldCheck } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/checkbox';
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select';

type Book = { id: string; mode: string; approval_required: boolean; account_id: string; symbols: string[]; max_order_notional: number; max_daily_notional: number };
type Proposal = { id: string; book: string; symbol: string; side: string; quantity: number; limit_price: number; rationale: string; state: string; quote_at: string };
type State = { halted: boolean; live_armed: boolean; books: Book[]; proposals: Proposal[] };
const money = (n: number) => new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 2}).format(n);

export default function Console() {
  const [token, setToken] = useState('');
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [book, setBook] = useState('overnight-us');
  const [mode, setMode] = useState('shadow');
  const [account, setAccount] = useState('local-shadow');
  const [symbols, setSymbols] = useState('AAPL');
  const [orderCap, setOrderCap] = useState('1000');
  const [dailyCap, setDailyCap] = useState('5000');
  const [approval, setApproval] = useState(true);
  const [paperBook, setPaperBook] = useState('');
  const [paperStatus, setPaperStatus] = useState<object | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  useEffect(() => {
    type Tool = { name: string; description: string; inputSchema: object; annotations: object; execute: (input: unknown) => object };
    const context = (document as Document & {modelContext?: {registerTool: (tool: Tool, options: {signal: AbortSignal}) => unknown}}).modelContext;
    if (!context?.registerTool) return;
    const lifecycle = new AbortController();
    try {
      void Promise.resolve(context.registerTool({
        name: 'get_control_summary', description: 'Read the displayed connection, halt state and book counts. Cannot approve or submit trades.',
        inputSchema: {type: 'object', properties: {}, additionalProperties: false},
        annotations: {readOnlyHint: true, untrustedContentHint: false},
        execute(input) {
          if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).length) throw new Error('Expected an empty object');
          const current = stateRef.current;
          return {connected: !!current, halted: current?.halted ?? null, live_armed: current?.live_armed ?? false,
            books: current?.books.length ?? 0, pending: current?.proposals.filter(p=>p.state==='pending').length ?? 0};
        },
      }, {signal: lifecycle.signal})).catch(()=>console.warn('Optional read-only agent tool unavailable'));
    } catch { console.warn('Optional read-only agent tool unavailable'); }
    return () => lifecycle.abort();
  }, []);

  async function request<T extends object = Record<string, unknown>>(path: string, payload?: object): Promise<T> {
    const res = await fetch('/api/' + path, {method: payload ? 'POST' : 'GET',
      headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
      ...(payload ? {body: JSON.stringify(payload)} : {})});
    const result = await res.json();
    if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error('Invalid service response');
    if (!res.ok) throw new Error('error' in result && typeof result.error === 'string' ? result.error : 'Request failed');
    return result as T;
  }
  async function act(path: string, payload?: object) {
    setBusy(true); setError('');
    try {
      await request(path, payload);
      setState(await request<State>('status'));
    } catch (e) { setError(e instanceof Error ? e.message : 'Unable to reach the private service'); }
    finally { setBusy(false); }
  }
  function edit(b: Book) {
    setBook(b.id); setMode(b.mode); setAccount(b.account_id); setSymbols(b.symbols.join(', '));
    setOrderCap(String(b.max_order_notional)); setDailyCap(String(b.max_daily_notional)); setApproval(b.approval_required);
  }

  return <main className="console-shell">
    <header className="masthead"><div className="brand"><Activity size={23}/><span>Honest Alpha Lab<span className="brand-sub">CONTROL ROOM</span></span></div>
      <span className="connection"><LockKeyhole size={15}/>{state ? 'Private operator session' : 'Not connected'}</span></header>
    <div className="console-content">
      <div className="page-heading"><div><p className="eyebrow">Research to execution</p><h1>Your books.<br/>Your final call.</h1></div>
        <div className="safety-panel"><ShieldCheck size={21}/><div><strong>{state?.live_armed ? 'Live deployment is armed' : 'Live execution is locked'}</strong><p>{state?.halted !== false ? 'All dispatch is paused.' : 'Dispatch is enabled under each book’s policy.'} Approving a proposal does not send it.</p></div></div></div>

      <section className="connection-bar" aria-label="Operator connection"><label htmlFor="token">Operator access</label>
        <Input id="token" type="password" autoComplete="off" value={token} onChange={e=>setToken(e.target.value)} placeholder="Private console token"/>
        <Button disabled={busy || !token} onClick={()=>act('status')}>{state ? 'Refresh' : 'Connect'}</Button>
        {state && <Button variant="outline" onClick={()=>{setToken('');setState(null);setPaperStatus(null);}}>Disconnect</Button>}
        {state && <Button variant="destructive" disabled={busy} onClick={()=>act('halt',{halted: !state.halted})}><Pause size={15}/>{state.halted ? 'Resume dispatch' : 'Halt all dispatch'}</Button>}
      </section>
      {error && <p className="error-banner" role="alert">{error}</p>}

      <div className="workspace-grid"><section className="main-column">
        <div className="section-heading"><h2>Trade inbox</h2><span>{state?.proposals.filter(p=>p.state==='pending').length ?? 0} awaiting review</span></div>
        {!state?.proposals.length ? <div className="empty-panel"><ArrowUpRight size={28}/><h3>No proposals to review</h3><p>{state ? 'Your strategy service can submit trades here. Every proposal carries a book, limit price and rationale.' : 'Connect to your private service to see actual books and proposed trades. No demo trades are mixed into this view.'}</p></div> :
          <div className="trade-list">{state.proposals.map(p=><article className="trade-row" key={p.id}>
            <div className="trade-top"><div><span className="trade-symbol">{p.symbol}</span><span className="trade-side">{p.side} · {p.quantity} shares</span></div><span className={'status '+p.state}>{p.state.replaceAll('_',' ')}</span></div>
            <div className="trade-details"><span>{p.book}</span><strong>{money(p.limit_price)} limit</strong><span>{money(p.quantity*p.limit_price)} notional</span></div>
            <p>{p.rationale}</p><small>Quote: {p.quote_at} · ID: {p.id}</small>
            <div className="trade-actions">{p.state==='pending' && <><Button disabled={busy} onClick={()=>act('review',{id:p.id,approve:true})}>Approve intent</Button><Button variant="outline" disabled={busy} onClick={()=>act('review',{id:p.id,approve:false})}>Reject</Button></>}
              {(p.state==='approved' || p.state==='pending' && state.books.some(b=>b.id===p.book&&!b.approval_required)) && <Button disabled={busy || state.halted} variant="outline" onClick={()=>{if(state.books.some(b=>b.id===p.book&&b.mode==='live')&&!window.confirm(`REAL ORDER: ${p.side} ${p.quantity} ${p.symbol} at ${money(p.limit_price)} limit in ${p.book}. Submit to Webull?`)) return; void act('dispatch',{id:p.id});}}>Send under book policy <ArrowUpRight size={15}/></Button>}</div>
          </article>)}</div>}
        <div className="section-heading books-heading"><h2>Execution books</h2><span>Independent policies</span></div>
        {!state?.books.length ? <p className="muted-text">No execution books configured. Start with a shadow book.</p> : <div className="book-list">{state.books.map(b=><button className="book-row" key={b.id} onClick={()=>edit(b)}><div><strong>{b.id}</strong><span>{b.symbols.join(' · ')}</span></div><div><strong className={'mode-label '+b.mode}>{b.mode}</strong><span>{b.approval_required ? 'Approval required' : 'Unattended policy'} · {money(b.max_daily_notional)}/day</span></div></button>)}</div>}
      </section><aside className="policy-column"><h2>Book policy</h2><p className="muted-text">Changes invalidate outstanding proposals. Live mode also requires a separate server-side unlock.</p>
        <label htmlFor="book">Book name</label><Input id="book" value={book} onChange={e=>setBook(e.target.value)}/>
        <label>Execution environment</label><Select value={mode} onValueChange={v=>setMode(v ?? 'shadow')}><SelectTrigger className="w-full" aria-label="Execution environment"><SelectValue/></SelectTrigger><SelectContent>{['shadow','paper','live'].map(m=><SelectItem key={m} value={m}>{m}</SelectItem>)}</SelectContent></Select>
        <label htmlFor="account">Account identifier</label><Input id="account" value={account} onChange={e=>setAccount(e.target.value)}/>
        <label htmlFor="symbols">Allowed US symbols</label><Input id="symbols" value={symbols} onChange={e=>setSymbols(e.target.value)}/>
        <div className="limits-grid"><div><label htmlFor="order-cap">Per order · USD</label><Input id="order-cap" type="number" min="1" value={orderCap} onChange={e=>setOrderCap(e.target.value)}/></div><div><label htmlFor="daily-cap">Per day · USD</label><Input id="daily-cap" type="number" min="1" value={dailyCap} onChange={e=>setDailyCap(e.target.value)}/></div></div>
        <label className="check-label"><Checkbox checked={approval} onCheckedChange={v=>setApproval(v===true)}/>Require my approval</label>
        <Button className="w-full" disabled={!state || busy} onClick={()=>act('books',{id:book,policy:{mode,account_id:account,symbols:symbols.split(',').map(s=>s.trim()).filter(Boolean),approval_required:approval,max_order_notional:Number(orderCap),max_daily_notional:Number(dailyCap)}})}>Save book policy</Button>
        <div className="paper-status"><h3>Paper book ledger</h3><label htmlFor="paper-book">Existing paper book ID</label><Input id="paper-book" value={paperBook} onChange={e=>setPaperBook(e.target.value)}/><Button variant="outline" className="w-full" disabled={!state || !paperBook || busy} onClick={async()=>{try{setPaperStatus(await request('paper-status?book='+encodeURIComponent(paperBook)));}catch(e){setError(e instanceof Error?e.message:'Unable to read ledger');}}}>Read ledger</Button>{paperStatus && <pre>{JSON.stringify(paperStatus,null,2)}</pre>}</div>
      </aside></div>
      <footer>Daily, overnight and session-timed research books · No broker secrets in this browser · Pausing does not cancel already submitted orders</footer>
    </div>
  </main>;
}
