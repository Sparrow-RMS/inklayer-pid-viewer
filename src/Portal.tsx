import { useState, type FormEvent, type ReactNode } from 'react'
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
      <header className="showcase-header">
        <div aria-label="Sparrow logo placeholder" className="sparrow-logo-slot" role="img">SPARROW LOGO</div>
      </header>
      <div className="login-showcase-copy">
        <p className="showcase-kicker">PROCESS SAFETY · OPERATIONS</p>
        <h2>Process safety,<br />structured.</h2>
        <p>Bring facility risk reviews and safety workflows together in one place.</p>
      </div>
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

function AddMemberDialog({ onClose, onAdd }: { onClose: () => void; onAdd: (nameOrEmail: string, role: Role) => void }) {
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    onAdd(String(form.get('user') ?? '').trim(), String(form.get('role') ?? 'Member') as Role)
  }
  return <div className="dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section aria-labelledby="add-member-title" aria-modal="true" className="member-dialog" role="dialog">
      <header className="dialog-header"><h2 id="add-member-title"><Symbol>person_add</Symbol>Add Member</h2><button aria-label="Close dialog" className="icon-button" onClick={onClose} type="button"><Symbol>close</Symbol></button></header>
      <form className="dialog-form" onSubmit={submit}>
        <label htmlFor="invite-user">USER</label><input autoFocus id="invite-user" name="user" placeholder="Enter user name or email address..." required />
        <label htmlFor="invite-role">ROLE</label><select defaultValue="Member" id="invite-role" name="role"><option>Admin</option><option>Member</option><option>Viewer</option></select>
        <div className="dialog-actions">
          <button className="button-secondary" onClick={onClose} type="button">Cancel</button>
          <button className="button-primary" type="submit">Submit</button>
        </div>
      </form>
    </section>
  </div>
}

function Dashboard({ userName, onSignOut }: { userName: string; onSignOut: () => void }) {
  const [section, setSection] = useState<Section>('Team')
  const [applicationOpened, setApplicationOpened] = useState(false)
  const [members, setMembers] = useState<Member[]>([])
  const [dialogOpen, setDialogOpen] = useState(false)
  const [openMenu, setOpenMenu] = useState<number | null>(null)
  const [filterOpen, setFilterOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [roleFilter, setRoleFilter] = useState('All roles')
  const [profileOpen, setProfileOpen] = useState(false)
  const [notice, setNotice] = useState('')
  const filteredMembers = members.filter((member) => `${member.name} ${member.title} ${member.email}`.toLowerCase().includes(query.toLowerCase()) && (roleFilter === 'All roles' || member.role === roleFilter))

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
    setSection(nextSection)
    if (nextSection === 'Application') setApplicationOpened(true)
  }

  return <DashboardFrame userName={userName} section={section} onSectionChange={handleSectionChange} onSignOut={onSignOut} profileOpen={profileOpen} setProfileOpen={setProfileOpen}>
    <main className="team-content" hidden={section !== 'Team'}>
      <header className="team-heading-row"><div className="team-title"><h1>Team</h1><span className="member-count">{members.length} members</span></div><button className="button-primary add-member-button" onClick={() => setDialogOpen(true)} type="button"><Symbol>add</Symbol>Add Member</button></header>
      {notice && <div className="notice-bar" role="status"><Symbol>check_circle</Symbol>{notice}<button aria-label="Dismiss notice" onClick={() => setNotice('')} type="button"><Symbol>close</Symbol></button></div>}
      <div className="roster-tools"><button aria-expanded={filterOpen} aria-label="Toggle roster filters" className={`icon-button filter-toggle${filterOpen ? ' active' : ''}`} onClick={() => setFilterOpen((value) => !value)} type="button"><Symbol>filter_list</Symbol></button>
        {filterOpen && <div className="filter-controls"><label className="roster-search"><Symbol>search</Symbol><input aria-label="Search team members" onChange={(event) => setQuery(event.target.value)} placeholder="Search members" value={query} /></label><select aria-label="Filter by role" onChange={(event) => setRoleFilter(event.target.value)} value={roleFilter}><option>All roles</option><option>Admin</option><option>Member</option><option>Viewer</option></select></div>}
      </div>
      {filteredMembers.length ? <div className="roster-table-wrap"><table className="roster-table"><thead><tr><th scope="col">Name</th><th scope="col">Email</th><th scope="col">Role</th><th scope="col">Actions</th></tr></thead><tbody>
        {filteredMembers.map((member) => <tr key={member.id}><td><div className="member-identity"><span className={`avatar${member.id === 1 ? ' avatar-admin' : ''}`}>{member.name.split(/\s+/).map((part) => part[0]).join('').slice(0, 2)}</span><span><strong>{member.name}</strong><small>{member.title}</small></span></div></td><td className="member-email">{member.email}</td><td><span className={`role-badge role-${member.role.toLowerCase()}`}><i />{member.role}</span></td>
          <td className="action-cell"><div className="action-menu-wrap"><button aria-expanded={openMenu === member.id} aria-label={`Actions for ${member.name}`} className="icon-button row-menu-button" onClick={() => setOpenMenu(openMenu === member.id ? null : member.id)} type="button"><Symbol>more_vert</Symbol></button>
            {openMenu === member.id && <div className="row-action-menu"><span className="menu-caption">Set role</span>{(['Admin', 'Member', 'Viewer'] as Role[]).map((role) => <button key={role} onClick={() => setMemberRole(member.id, role)} type="button">{role}{member.role === role && <Symbol>check</Symbol>}</button>)}<button onClick={() => { setNotice(`Showing CAD revision and checkout history for ${member.name}.`); setOpenMenu(null) }} type="button">View history</button><button className="remove-action" onClick={() => { if (window.confirm(`Remove ${member.name} from Refinery Unit 04-A?`)) setMembers((current) => current.filter((item) => item.id !== member.id)); setOpenMenu(null) }} type="button">Remove member</button></div>}
          </div></td></tr>)}
      </tbody></table></div> : <div className="roster-empty"><span className="application-icon"><Symbol>group</Symbol></span><h2>{members.length ? 'No team members found' : 'No members yet'}</h2><p>{members.length ? 'Try changing the search or role filter.' : 'Invite teammates to join this workspace.'}</p>{members.length === 0 && <button className="button-primary" onClick={() => setDialogOpen(true)} type="button"><Symbol>person_add</Symbol>Add a member</button>}</div>}
      <footer className="roster-footer"><span>{filteredMembers.length} of {members.length} members</span><span>Workspace access is managed by your team administrator.</span></footer>
    </main>
    {applicationOpened && <div className="application-workspace pdf-application" hidden={section !== 'Application'}><PidViewer embedded /></div>}
    {dialogOpen && section === 'Team' && <AddMemberDialog onClose={() => setDialogOpen(false)} onAdd={addMember} />}
  </DashboardFrame>
}

function DashboardFrame({ children, userName, section, onSectionChange, onSignOut, profileOpen, setProfileOpen }: { children: ReactNode; userName: string; section: Section; onSectionChange: (section: Section) => void; onSignOut: () => void; profileOpen: boolean; setProfileOpen: (open: boolean) => void }) {
  return <div className="dashboard-shell">
    <header className="dashboard-header"><div className="header-product"><Brand compact /><span className="header-product-title">Inklayer P&amp;ID</span></div>
      <div className="profile-wrap"><button aria-expanded={profileOpen} className="profile-button" onClick={() => setProfileOpen(!profileOpen)} type="button"><span className="avatar profile-avatar">{initialsForName(userName)}</span><span><strong>{userName}</strong><small>Signed-in user</small></span><Symbol>arrow_drop_down</Symbol></button>
        {profileOpen && <div className="profile-menu"><span>Signed in as {userName}</span><button onClick={() => { setProfileOpen(false); onSignOut() }} type="button"><Symbol>logout</Symbol>Sign out</button></div>}
      </div>
    </header>
    <div className="dashboard-body"><aside className="dashboard-sidebar">
      <nav aria-label="Main navigation" className="sidebar-nav"><button aria-current={section === 'Team' ? 'page' : undefined} className={section === 'Team' ? 'selected' : ''} onClick={() => onSectionChange('Team')} type="button"><Symbol>groups</Symbol>Team</button><button aria-current={section === 'Application' ? 'page' : undefined} className={section === 'Application' ? 'selected' : ''} onClick={() => onSectionChange('Application')} type="button"><Symbol>schema</Symbol>Application</button></nav>
    </aside><div className="dashboard-main">{children}</div></div>
  </div>
}

export default function Portal() {
  const [screen, setScreen] = useState<'login' | 'dashboard'>('login')
  const [userName, setUserName] = useState('')
  if (screen === 'login') return <LoginScreen onSignIn={(name) => { setUserName(name); setScreen('dashboard') }} />
  return <Dashboard userName={userName} onSignOut={() => setScreen('login')} />
}