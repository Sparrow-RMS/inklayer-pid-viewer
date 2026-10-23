import { useState, type FormEvent, type ReactNode } from 'react'
import { Navigate, Outlet, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import PidViewer from './App'
import './Portal.css'

type Role = 'Admin' | 'Member' | 'Viewer'
type Section = 'Team' | 'Application'
type Member = { id: number; name: string; title: string; email: string; role: Role }

function displayNameForCredential(credential: string) {
  const name = credential.trim().split('@')[0].replace(/[._-]+/g, ' ')
  return name.split(/\s+/).filter(Boolean).map((part) => part[0].toUpperCase() + part.slice(1).toLowerCase()).join(' ')
}

function initialsForName(name: string) {
  return name.split(/\s+/).filter(Boolean).map((part) => part[0]).join('').slice(0, 2).toUpperCase()
}

function Symbol({ children, className = '' }: { children: string; className?: string }) {
  return <span aria-hidden="true" className={`material-symbol ${className}`}>{children}</span>
}

function Brand({ compact = false }: { compact?: boolean }) {
  return <div className={`brand${compact ? ' brand-compact' : ''}`}>
    <span className="brand-mark"><Symbol>account_tree</Symbol></span>
    <span className="brand-copy"><strong>Inklayer</strong><small>P&amp;ID VIEWER V4.2</small></span>
  </div>
}

function LoginScreen({ onSignIn }: { onSignIn: (name: string) => void }) {
  const [showPassword, setShowPassword] = useState(false)
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    onSignIn(displayNameForCredential(String(form.get('credential') ?? '')))
  }

  return <main className="login-screen">
    <section className="login-showcase">
      <div aria-hidden="true" className="showcase-glow" />
      <div aria-hidden="true" className="showcase-grid" />
      <header className="showcase-header">
        <img alt="Sparrow" className="sparrow-logo" src="/sparrow-logo.png" />
        <span className="showcase-pill"><i />New · Your P&amp;ID as a 3D shop floor</span>
      </header>

      <div className="login-showcase-copy">
        <p className="showcase-kicker">Process safety · HAZOP</p>
        <h2>HAZOP studies,<br /><span>drafted in hours.</span></h2>
        <p>Drop in a P&amp;ID. Inklayer divides it into nodes, drafts every worksheet, turns the drawing into a 3D model of your shop floor and keeps your team reviewing in one place.</p>
      </div>

      <figure aria-hidden="true" className="showcase-preview">
        <div className="preview-bar"><span /><span /><span /><em>P&amp;ID-104 · Crude Feed Section</em></div>
        <svg className="preview-drawing" viewBox="0 0 420 200">
          <g className="drawing-base">
            <rect height="70" rx="10" width="44" x="34" y="64" />
            <circle cx="186" cy="150" r="16" />
            <rect height="56" rx="22" width="44" x="330" y="40" />
            <path d="M178 48 l16 10 l-16 10 z M210 48 l-16 10 l16 10 z" />
          </g>
          <path className="node-run node-a" d="M78 80 H150 V58 H250 V68 H330" />
          <path className="node-run node-b" d="M78 118 H130 V150 H170" />
          <path className="node-run node-c" d="M202 150 H300 V96 H352" />
          <g className="node-tag" transform="translate(236 26)"><rect height="20" rx="6" width="58" /><text x="29" y="14">NODE 01</text></g>
          <g className="node-tag tag-c" transform="translate(222 122)"><rect height="20" rx="6" width="58" /><text x="29" y="14">NODE 03</text></g>
        </svg>
        <div className="preview-chip">
          <span className="chip-icon"><Symbol>auto_awesome</Symbol></span>
          <span><strong>Node 03 · No flow</strong><small>4 causes · 3 safeguards drafted</small></span>
        </div>
      </figure>

      <ul className="showcase-points">
        <li><Symbol>account_tree</Symbol><span><strong>Auto nodes</strong>Whole pipe runs, painted on the drawing</span></li>
        <li><Symbol>table_chart</Symbol><span><strong>Draft worksheets</strong>Deviations, causes and safeguards</span></li>
        <li><Symbol>forum</Symbol><span><strong>Review together</strong>Edit by hand or through chat</span></li>
        <li className="showcase-point-new"><Symbol>view_in_ar</Symbol><span><strong>3D shop floor <em>New</em></strong>Walk the plant in 3D, every node in colour</span></li>
      </ul>
    </section>

    <section className="login-form-side">
      <div className="login-form-wrap">
        <header className="login-heading">
          <h1>Sign in</h1>
          <p>Welcome back. Enter your account details to continue.</p>
        </header>
        <form className="sign-in-form" onSubmit={submit}>
          <label htmlFor="credential">Email or Username</label>
          <div className="login-input-wrap"><Symbol>person</Symbol><input id="credential" name="credential" placeholder="name@company.com or username" required type="text" /></div>
          <label htmlFor="password">Password</label>
          <div className="login-input-wrap"><Symbol>lock</Symbol><input id="password" name="password" placeholder="Enter your password" required type={showPassword ? 'text' : 'password'} /><button aria-label={showPassword ? 'Hide password' : 'Show password'} className="password-toggle" type="button" onClick={() => setShowPassword((value) => !value)}><Symbol>{showPassword ? 'visibility_off' : 'visibility'}</Symbol></button></div>
          <button className="sign-in-button" type="submit">Sign in <Symbol>arrow_forward</Symbol></button>
        </form>
      </div>
    </section>
  </main>
}

const ROLES: Role[] = ['Admin', 'Member', 'Viewer']
const ROLE_DETAILS: Record<Role, { icon: string; blurb: string }> = {
  Admin: { icon: 'shield_person', blurb: 'Manages members, roles and workspace settings.' },
  Member: { icon: 'edit_note', blurb: 'Runs the pipeline, edits nodes and drafts worksheets.' },
  Viewer: { icon: 'visibility', blurb: 'Opens drawings and reads worksheets without editing.' },
}
const AVATAR_TONES = 6

function avatarTone(name: string) {
  let hash = 0
  for (const char of name) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
  return `tone-${hash % AVATAR_TONES}`
}

function Avatar({ name, className = '' }: { name: string; className?: string }) {
  return <span className={`avatar ${avatarTone(name)} ${className}`}>{initialsForName(name)}</span>
}

function AddMemberDialog({ onClose, onAdd }: { onClose: () => void; onAdd: (nameOrEmail: string, role: Role) => void }) {
  const [role, setRole] = useState<Role>('Member')
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    onAdd(String(form.get('user') ?? '').trim(), role)
  }
  return <div className="dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section aria-labelledby="add-member-title" aria-modal="true" className="member-dialog" role="dialog">
      <header className="dialog-header">
        <span className="dialog-icon"><Symbol>person_add</Symbol></span>
        <div><h2 id="add-member-title">Invite a teammate</h2><p>They'll get access to this workspace's drawings and worksheets.</p></div>
        <button aria-label="Close dialog" className="icon-button" onClick={onClose} type="button"><Symbol>close</Symbol></button>
      </header>
      <form className="dialog-form" onSubmit={submit}>
        <label htmlFor="invite-user">Name or email</label>
        <div className="field-wrap"><Symbol>alternate_email</Symbol><input autoFocus id="invite-user" name="user" placeholder="jane@company.com" required /></div>
        <span className="field-label" id="invite-role-label">Role</span>
        <div aria-labelledby="invite-role-label" className="role-picker" role="radiogroup">
          {ROLES.map((option) => <button aria-checked={role === option} className={`role-option role-${option.toLowerCase()}${role === option ? ' selected' : ''}`} key={option} onClick={() => setRole(option)} role="radio" type="button">
            <Symbol>{ROLE_DETAILS[option].icon}</Symbol><strong>{option}</strong><small>{ROLE_DETAILS[option].blurb}</small>
          </button>)}
        </div>
        <div className="dialog-actions">
          <button className="button-secondary" onClick={onClose} type="button">Cancel</button>
          <button className="button-primary" type="submit"><Symbol>send</Symbol>Send invite</button>
        </div>
      </form>
    </section>
  </div>
}

function Dashboard({ userName, onSignOut }: { userName: string; onSignOut: () => void }) {
  const location = useLocation()
  const navigate = useNavigate()
  const section: Section = location.pathname === '/viewer' ? 'Application' : 'Team'
  const [applicationOpened, setApplicationOpened] = useState(() => location.pathname === '/viewer')
  const [members, setMembers] = useState<Member[]>([])
  const [dialogOpen, setDialogOpen] = useState(false)
  const [openMenu, setOpenMenu] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const [roleFilter, setRoleFilter] = useState<Role | 'All'>('All')
  const [profileOpen, setProfileOpen] = useState(false)
  const [notice, setNotice] = useState('')
  const filteredMembers = members.filter((member) => `${member.name} ${member.title} ${member.email}`.toLowerCase().includes(query.toLowerCase()) && (roleFilter === 'All' || member.role === roleFilter))
  const countFor = (role: Role) => members.filter((member) => member.role === role).length

  const addMember = (nameOrEmail: string, role: Role) => {
    const isEmail = nameOrEmail.includes('@')
    const name = isEmail ? nameOrEmail.split('@')[0].replace(/[._-]/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()) : nameOrEmail
    const email = isEmail ? nameOrEmail : `${name.toLowerCase().replace(/\s+/g, '.')}@inklayer-demo.com`
    setMembers((current) => [...current, { id: Date.now(), name, title: 'Invited member', email, role }])
    setDialogOpen(false)
    setNotice(`Invitation sent to ${email} as ${role}.`)
  }
  const setMemberRole = (id: number, role: Role) => {
    setMembers((current) => current.map((member) => member.id === id ? { ...member, role } : member))
    setOpenMenu(null)
  }
  const handleSectionChange = (nextSection: Section) => {
    navigate(nextSection === 'Team' ? '/team' : '/viewer')
    if (nextSection === 'Application') setApplicationOpened(true)
  }

  const stats = [
    { label: 'Total members', value: members.length, icon: 'groups', tone: 'indigo' },
    { label: 'Admins', value: countFor('Admin'), icon: 'shield_person', tone: 'violet' },
    { label: 'Members', value: countFor('Member'), icon: 'edit_note', tone: 'teal' },
    { label: 'Viewers', value: countFor('Viewer'), icon: 'visibility', tone: 'amber' },
  ]

  return <DashboardFrame memberCount={members.length} userName={userName} section={section} onSectionChange={handleSectionChange} onSignOut={onSignOut} profileOpen={profileOpen} setProfileOpen={setProfileOpen}>
    <main className="team-content" hidden={section !== 'Team'}>
      <header className="team-hero">
        <div aria-hidden="true" className="team-hero-glow" />
        <div className="team-title">
          <p className="eyebrow">Workspace</p>
          <h1>Team</h1>
          <p>Invite the people who review drawings and sign off worksheets.</p>
        </div>
        <button className="button-primary add-member-button" onClick={() => setDialogOpen(true)} type="button"><Symbol>add</Symbol>Invite member</button>
      </header>

      <section aria-label="Team summary" className="stat-grid">
        {stats.map((stat) => <article className={`stat-card stat-${stat.tone}`} key={stat.label}>
          <span className="stat-icon"><Symbol>{stat.icon}</Symbol></span>
          <span className="stat-copy"><small>{stat.label}</small><strong>{stat.value}</strong></span>
        </article>)}
      </section>

      {notice && <div className="notice-bar" role="status"><Symbol>check_circle</Symbol>{notice}<button aria-label="Dismiss notice" onClick={() => setNotice('')} type="button"><Symbol>close</Symbol></button></div>}

      {members.length ? <section className="roster-card">
        <div className="roster-tools">
          <label className="roster-search"><Symbol>search</Symbol><input aria-label="Search team members" onChange={(event) => setQuery(event.target.value)} placeholder="Search by name or email" value={query} /></label>
          <div aria-label="Filter by role" className="segmented" role="tablist">
            {(['All', ...ROLES] as const).map((option) => <button aria-selected={roleFilter === option} className={roleFilter === option ? 'active' : ''} key={option} onClick={() => setRoleFilter(option)} role="tab" type="button">
              {option}<span>{option === 'All' ? members.length : countFor(option)}</span>
            </button>)}
          </div>
        </div>
        {filteredMembers.length ? <div className="roster-table-wrap"><table className="roster-table"><thead><tr><th scope="col">Member</th><th scope="col">Email</th><th scope="col">Role</th><th scope="col">Status</th><th aria-label="Actions" scope="col" /></tr></thead><tbody>
          {filteredMembers.map((member) => <tr key={member.id}>
            <td><div className="member-identity"><Avatar name={member.name} /><span><strong>{member.name}</strong><small>{member.title}</small></span></div></td>
            <td className="member-email">{member.email}</td>
            <td><span className={`role-badge role-${member.role.toLowerCase()}`}><Symbol>{ROLE_DETAILS[member.role].icon}</Symbol>{member.role}</span></td>
            <td><span className="status-pill"><i />Invite pending</span></td>
            <td className="action-cell"><div className="action-menu-wrap"><button aria-expanded={openMenu === member.id} aria-label={`Actions for ${member.name}`} className="icon-button row-menu-button" onClick={() => setOpenMenu(openMenu === member.id ? null : member.id)} type="button"><Symbol>more_horiz</Symbol></button>
              {openMenu === member.id && <div className="row-action-menu"><span className="menu-caption">Set role</span>{ROLES.map((role) => <button key={role} onClick={() => setMemberRole(member.id, role)} type="button"><span><Symbol>{ROLE_DETAILS[role].icon}</Symbol>{role}</span>{member.role === role && <Symbol className="menu-check">check</Symbol>}</button>)}<hr /><button onClick={() => { setNotice(`Showing CAD revision and checkout history for ${member.name}.`); setOpenMenu(null) }} type="button"><span><Symbol>history</Symbol>View history</span></button><button className="remove-action" onClick={() => { if (window.confirm(`Remove ${member.name} from Refinery Unit 04-A?`)) setMembers((current) => current.filter((item) => item.id !== member.id)); setOpenMenu(null) }} type="button"><span><Symbol>person_remove</Symbol>Remove member</span></button></div>}
            </div></td>
          </tr>)}
        </tbody></table></div> : <div className="roster-no-match"><Symbol>search_off</Symbol><strong>No one matches that search</strong><span>Try another name or role filter.</span></div>}
        <footer className="roster-footer"><span>Showing {filteredMembers.length} of {members.length}</span><span><Symbol>lock</Symbol>Access is managed by your workspace admin.</span></footer>
      </section> : <section className="roster-empty">
        <div aria-hidden="true" className="empty-avatars"><span className="avatar tone-0">AK</span><span className="avatar tone-2">RS</span><span className="avatar tone-4">MJ</span><span className="empty-plus"><Symbol>add</Symbol></span></div>
        <h2>Build your review team</h2>
        <p>HAZOP is a team sport. Invite your process engineers, operators and safety leads to review nodes and worksheets together.</p>
        <button className="button-primary" onClick={() => setDialogOpen(true)} type="button"><Symbol>person_add</Symbol>Invite your first teammate</button>
        <div className="role-cards">
          {ROLES.map((role) => <article className={`role-card role-${role.toLowerCase()}`} key={role}><Symbol>{ROLE_DETAILS[role].icon}</Symbol><strong>{role}</strong><span>{ROLE_DETAILS[role].blurb}</span></article>)}
        </div>
      </section>}
    </main>
    {applicationOpened && <div className="application-workspace pdf-application" hidden={section !== 'Application'}><PidViewer embedded /></div>}
    {dialogOpen && section === 'Team' && <AddMemberDialog onClose={() => setDialogOpen(false)} onAdd={addMember} />}
    <Outlet />
  </DashboardFrame>
}

function DashboardFrame({ children, memberCount, userName, section, onSectionChange, onSignOut, profileOpen, setProfileOpen }: { children: ReactNode; memberCount: number; userName: string; section: Section; onSectionChange: (section: Section) => void; onSignOut: () => void; profileOpen: boolean; setProfileOpen: (open: boolean) => void }) {
  const navItems: { id: Section; icon: string; label: string; hint: string }[] = [
    { id: 'Team', icon: 'groups', label: 'Team', hint: `${memberCount}` },
    { id: 'Application', icon: 'schema', label: 'P&ID Viewer', hint: 'App' },
  ]
  return <div className="dashboard-shell">
    <header className="dashboard-header">
      <div className="header-product"><Brand compact /><span className="header-divider" /><nav aria-label="Breadcrumb" className="breadcrumb"><span>Workspace</span><Symbol>chevron_right</Symbol><strong>{section === 'Team' ? 'Team' : 'P&ID Viewer'}</strong></nav></div>
      <div className="profile-wrap"><button aria-expanded={profileOpen} className="profile-button" onClick={() => setProfileOpen(!profileOpen)} type="button"><Avatar className="profile-avatar" name={userName} /><span><strong>{userName}</strong><small>Workspace owner</small></span><Symbol>expand_more</Symbol></button>
        {profileOpen && <div className="profile-menu"><div className="profile-menu-head"><Avatar name={userName} /><span><strong>{userName}</strong><small>Signed in</small></span></div><button onClick={() => { setProfileOpen(false); onSignOut() }} type="button"><Symbol>logout</Symbol>Sign out</button></div>}
      </div>
    </header>
    <div className="dashboard-body">
      <aside className="dashboard-sidebar">
        <p className="sidebar-section-label">Workspace</p>
        <nav aria-label="Main navigation" className="sidebar-nav">
          {navItems.map((item) => <button aria-current={section === item.id ? 'page' : undefined} className={section === item.id ? 'selected' : ''} key={item.id} onClick={() => onSectionChange(item.id)} type="button">
            <span className="nav-icon"><Symbol>{item.icon}</Symbol></span>{item.label}<span className="nav-hint">{item.hint}</span>
          </button>)}
        </nav>
        {section === 'Team' && <div className="sidebar-card">
          <span className="sidebar-card-icon"><Symbol>auto_awesome</Symbol></span>
          <strong>Run your first study</strong>
          <span>Open a P&amp;ID and let the agent divide it into HAZOP nodes, then see them on a 3D model of your shop floor.</span>
          <button onClick={() => onSectionChange('Application')} type="button">Open viewer<Symbol>arrow_forward</Symbol></button>
        </div>}
      </aside>
      <div className="dashboard-main">{children}</div>
    </div>
  </div>
}

export default function Portal() {
  const [userName, setUserName] = useState('')
  const navigate = useNavigate()
  const signIn = (name: string) => {
    setUserName(name)
    navigate('/team', { replace: true })
  }
  const signOut = () => {
    setUserName('')
    navigate('/login', { replace: true })
  }

  return <Routes>
    <Route path="/login" element={userName ? <Navigate to="/team" replace /> : <LoginScreen onSignIn={signIn} />} />
    <Route element={userName ? <Dashboard userName={userName} onSignOut={signOut} /> : <Navigate to="/login" replace />}>
      <Route index element={<Navigate to="/team" replace />} />
      <Route path="team" element={null} />
      <Route path="viewer" element={null} />
    </Route>
    <Route path="*" element={<Navigate to={userName ? '/team' : '/login'} replace />} />
  </Routes>
}