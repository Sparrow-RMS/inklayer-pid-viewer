import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { CSS2DObject, CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js'
import type { SceneItem, ScenePage } from './plantSceneApi'

/**
 * Draws one page of the plant scene (agent/src/hazop_agent/plant_scene.py) and
 * colours HAZOP nodes on it. Every mesh is built from a pipeline id (a step 1
 * entity or a traced span), so a node's members are found by id, never by
 * matching tags.
 *
 * Floor plan: page x -> world x (east), page y -> world z (south), in metres;
 * y is up. Pipes run at PIPE_Y along their drawn routes.
 */

export interface SceneNode {
  key: string
  color: string
  /** Step 1 entity ids and traced span ids of the node's members. */
  memberIds: string[]
}

export interface PickedObject {
  id: string
  kind: 'item' | 'span'
}

const PIPE_Y = 1.2
const FADED_OPACITY = 0.1
const FLY_MS = 700

function isDark() {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-color-scheme: dark)').matches
}

const BASE_COLORS = {
  equipment: '#9aa7b4',
  pipe: '#7d848c',
  valve: '#4a5560',
  instrument: '#e8e4d8',
  connector: '#b9a67a',
  fitting: '#6c757d',
}

export class PlantSceneView {
  private renderer: THREE.WebGLRenderer
  private labels: CSS2DRenderer
  private scene = new THREE.Scene()
  private camera: THREE.PerspectiveCamera
  private controls: OrbitControls
  private root = new THREE.Group()
  private container: HTMLElement
  private resizeObserver: ResizeObserver
  private frame = 0
  private flight: { from: THREE.Vector3; to: THREE.Vector3; targetFrom: THREE.Vector3; targetTo: THREE.Vector3; start: number } | null = null
  /** Pipeline id -> its meshes (an item can have several, e.g. a tank and its roof). */
  private meshes = new Map<string, THREE.Mesh[]>()
  private baseColor = new Map<THREE.Mesh, THREE.Color>()
  private nodeOfId = new Map<string, SceneNode>()
  private nodes: SceneNode[] = []
  private focused: string | null = null
  private pointerDown: { x: number; y: number } | null = null
  onPick: ((picked: PickedObject | null) => void) | null = null

  constructor(container: HTMLElement) {
    this.container = container
    this.renderer = new THREE.WebGLRenderer({ antialias: true })
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    this.renderer.setClearColor(isDark() ? '#15171c' : '#eef1f4')
    container.appendChild(this.renderer.domElement)

    this.labels = new CSS2DRenderer()
    this.labels.domElement.className = 'p3n-labels'
    container.appendChild(this.labels.domElement)

    this.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 5000)
    this.controls = new OrbitControls(this.camera, this.renderer.domElement)
    this.controls.enableDamping = true
    this.controls.maxPolarAngle = Math.PI / 2 - 0.02

    this.scene.add(new THREE.HemisphereLight('#ffffff', '#55606b', 1.6))
    const sun = new THREE.DirectionalLight('#ffffff', 1.4)
    sun.position.set(-60, 120, 40)
    this.scene.add(sun, this.root)

    this.renderer.domElement.addEventListener('pointerdown', this.handlePointerDown)
    this.renderer.domElement.addEventListener('pointerup', this.handlePointerUp)
    this.resizeObserver = new ResizeObserver(() => this.resize())
    this.resizeObserver.observe(container)
    this.resize()
    this.animate()
  }

  /** Replaces the scene with `page`. Node colours set earlier are reapplied. */
  load(page: ScenePage, keepCamera = false) {
    this.clear()
    const width = page.pageWidthM
    const depth = page.pageWidthM * page.aspect
    // Centre the sheet on the origin.
    this.root.position.set(-width / 2, 0, -depth / 2)

    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(width * 1.1, depth * 1.1),
      new THREE.MeshStandardMaterial({ color: isDark() ? '#22262d' : '#dde2e7', roughness: 1 }),
    )
    ground.rotation.x = -Math.PI / 2
    ground.position.set(width / 2, 0, depth / 2)
    const grid = new THREE.GridHelper(Math.max(width, depth) * 1.1, Math.round(Math.max(width, depth) / 5), '#8a939c', '#aab2ba')
    grid.position.set(width / 2, 0.01, depth / 2)
    ;(grid.material as THREE.Material).opacity = 0.25
    ;(grid.material as THREE.Material).transparent = true
    this.root.add(ground, grid)

    const pipeRadius = Math.max(0.06, width * 0.0012)
    for (const span of page.spans) {
      const points = span.points.map((p) => new THREE.Vector3(p.x * width, PIPE_Y, p.y * depth))
      const path = new THREE.CurvePath<THREE.Vector3>()
      for (let i = 1; i < points.length; i++) if (points[i].distanceTo(points[i - 1]) > 1e-4) path.add(new THREE.LineCurve3(points[i - 1], points[i]))
      if (!path.curves.length) continue
      const geometry = new THREE.TubeGeometry(path, Math.max(2, path.curves.length * 2), pipeRadius, 6, false)
      this.addMesh(span.id, geometry, BASE_COLORS.pipe)
    }

    for (const item of page.items) this.addItem(item, width, depth, pipeRadius)

    this.applyNodes()
    if (!keepCamera) this.frameBox(this.plantBox(), false)
  }

  setNodes(nodes: SceneNode[]) {
    this.nodes = nodes
    this.applyNodes()
  }

  /** Highlights one node (others fade) and flies to it; null shows every node. */
  focus(key: string | null) {
    this.focused = key
    this.applyNodes()
    if (key === null) {
      this.frameBox(this.plantBox(), true)
      return
    }
    const box = new THREE.Box3()
    for (const [id, node] of this.nodeOfId) if (node.key === key) for (const mesh of this.meshes.get(id) ?? []) box.expandByObject(mesh)
    if (!box.isEmpty()) this.frameBox(box, true)
  }

  /** The items and pipes, without the ground around them. */
  private plantBox() {
    const box = new THREE.Box3()
    for (const meshes of this.meshes.values()) for (const mesh of meshes) box.expandByObject(mesh)
    return box.isEmpty() ? new THREE.Box3().setFromObject(this.root) : box
  }

  dispose() {
    cancelAnimationFrame(this.frame)
    this.resizeObserver.disconnect()
    this.renderer.domElement.removeEventListener('pointerdown', this.handlePointerDown)
    this.renderer.domElement.removeEventListener('pointerup', this.handlePointerUp)
    this.clear()
    this.controls.dispose()
    this.renderer.dispose()
    this.renderer.domElement.remove()
    this.labels.domElement.remove()
  }

  // ───────────────────────────────────────────────────────── building

  private addItem(item: SceneItem, width: number, depth: number, pipeRadius: number) {
    const x = item.x * width
    const z = item.y * depth
    const footW = Math.max(item.w * width, 0.3)
    const footD = Math.max(item.h * depth, 0.3)
    const across = Math.min(footW, footD)
    const elevation = item.elevationM ?? PIPE_Y
    const color = BASE_COLORS[item.category]
    const add = (geometry: THREE.BufferGeometry, px: number, py: number, pz: number, rotate?: (m: THREE.Mesh) => void) => {
      const mesh = this.addMesh(item.id, geometry, color)
      mesh.position.set(px, py, pz)
      rotate?.(mesh)
      return mesh
    }

    switch (item.shape) {
      case 'tank':
      case 'vertical_vessel':
      case 'column':
      case 'filter': {
        const r = across / 2
        const h = Math.max(item.heightM, r)
        add(new THREE.CylinderGeometry(r, r, h, 32), x, elevation + h / 2, z)
        if (item.shape === 'tank') add(new THREE.ConeGeometry(r, r * 0.25, 32), x, elevation + h + r * 0.125, z)
        else {
          add(new THREE.SphereGeometry(r, 32, 12, 0, Math.PI * 2, 0, Math.PI / 2), x, elevation + h, z)
          if (elevation > 0.05) add(new THREE.SphereGeometry(r, 32, 12, 0, Math.PI * 2, Math.PI / 2, Math.PI / 2), x, elevation, z)
        }
        if (elevation > 0.3) this.addLegs(item.id, x, z, r * 0.8, elevation, color)
        break
      }
      case 'horizontal_vessel':
      case 'heat_exchanger': {
        const alongX = footW >= footD
        const length = Math.max(footW, footD)
        const r = Math.min(across / 2, Math.max(item.diameterM / 2, 0.3))
        const turn = (m: THREE.Mesh) => {
          if (alongX) m.rotation.z = Math.PI / 2
          else m.rotation.x = Math.PI / 2
        }
        const y = Math.max(elevation, 0) + r
        add(new THREE.CylinderGeometry(r, r, length, 32), x, y, z, turn)
        for (const end of [-1, 1]) {
          const cap = add(new THREE.SphereGeometry(r, 24, 12), x + (alongX ? (end * length) / 2 : 0), y, z + (alongX ? 0 : (end * length) / 2))
          cap.scale.set(alongX ? 0.35 : 1, 1, alongX ? 1 : 0.35)
        }
        if (y - r > 0.2) this.addLegs(item.id, x, z, Math.min(length / 2, across) * 0.7, y - r, color)
        break
      }
      case 'pump':
      case 'motor': {
        const s = Math.min(across, 2)
        const base = Math.max(elevation, 0)
        add(new THREE.BoxGeometry(s * 1.2, s * 0.25, s * 0.7), x, base + s * 0.125, z)
        add(new THREE.CylinderGeometry(s * 0.3, s * 0.3, s * 0.8, 20), x, base + s * 0.55, z, (m) => (m.rotation.z = Math.PI / 2))
        break
      }
      case 'agitator': {
        const r = Math.max(across * 0.08, 0.15)
        add(new THREE.CylinderGeometry(r * 0.3, r * 0.3, Math.max(item.heightM, 1), 8), x, elevation + Math.max(item.heightM, 1) / 2, z)
        add(new THREE.BoxGeometry(r * 3, r * 3, r * 3), x, elevation + Math.max(item.heightM, 1) + r * 1.5, z)
        break
      }
      case 'valve': {
        const s = pipeRadius * 3.5
        for (const side of [-1, 1]) add(new THREE.ConeGeometry(s, s * 1.4, 12), x + side * s * 0.7, PIPE_Y, z, (m) => (m.rotation.z = (side * Math.PI) / 2))
        add(new THREE.CylinderGeometry(s * 0.15, s * 0.15, s * 1.6, 8), x, PIPE_Y + s * 0.8, z)
        add(new THREE.CylinderGeometry(s * 0.7, s * 0.7, s * 0.15, 16), x, PIPE_Y + s * 1.6, z)
        break
      }
      case 'instrument': {
        const s = pipeRadius * 3
        add(new THREE.CylinderGeometry(s * 0.12, s * 0.12, 1.2, 6), x, PIPE_Y + 0.6, z)
        add(new THREE.CylinderGeometry(s, s, s * 0.6, 20), x, PIPE_Y + 1.2 + s, z, (m) => (m.rotation.x = Math.PI / 2))
        break
      }
      case 'connector': {
        const s = pipeRadius * 5
        add(new THREE.ConeGeometry(s, s * 2, 4), x, PIPE_Y, z, (m) => (m.rotation.z = -Math.PI / 2))
        break
      }
      case 'fitting':
        add(new THREE.SphereGeometry(pipeRadius * 1.8, 12, 8), x, PIPE_Y, z)
        break
      default: {
        const h = Math.max(item.heightM, 0.5)
        add(new THREE.BoxGeometry(footW, h, footD), x, Math.max(elevation, 0) + h / 2, z)
      }
    }

    // Equipment only: off-sheet connectors crowd the sheet's edges; clicking one shows its tag.
    if (item.tag && item.category === 'equipment') {
      const label = document.createElement('div')
      label.className = 'p3n-label'
      label.textContent = item.tag
      const object = new CSS2DObject(label)
      object.position.set(x, Math.max(elevation, 0) + Math.max(item.heightM, 1) + 1.5, z)
      object.userData.id = item.id
      this.root.add(object)
    }
  }

  private addLegs(id: string, x: number, z: number, spread: number, height: number, color: string) {
    for (const [dx, dz] of [[1, 1], [1, -1], [-1, 1], [-1, -1]]) {
      const leg = this.addMesh(id, new THREE.CylinderGeometry(0.08, 0.08, height, 6), color)
      leg.position.set(x + dx * spread * 0.7, height / 2, z + dz * spread * 0.7)
    }
  }

  private addMesh(id: string, geometry: THREE.BufferGeometry, color: string) {
    const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ color, roughness: 0.55, metalness: 0.25 }))
    mesh.userData.id = id
    this.baseColor.set(mesh, new THREE.Color(color))
    const list = this.meshes.get(id)
    if (list) list.push(mesh)
    else this.meshes.set(id, [mesh])
    this.root.add(mesh)
    return mesh
  }

  private clear() {
    this.root.traverse((object) => {
      if (object instanceof THREE.Mesh) {
        object.geometry.dispose()
        ;(object.material as THREE.Material).dispose()
      }
      if (object instanceof CSS2DObject) object.element.remove()
    })
    this.root.clear()
    this.meshes.clear()
    this.baseColor.clear()
  }

  // ───────────────────────────────────────────────────────── node colours

  private applyNodes() {
    this.nodeOfId.clear()
    // An item on a boundary belongs to two nodes; it takes the first's colour.
    for (const node of this.nodes) for (const id of node.memberIds) if (!this.nodeOfId.has(id)) this.nodeOfId.set(id, node)

    for (const [id, meshes] of this.meshes) {
      const node = this.nodeOfId.get(id)
      const faded = this.focused !== null && node?.key !== this.focused
      for (const mesh of meshes) {
        const material = mesh.material as THREE.MeshStandardMaterial
        material.color.copy(node ? new THREE.Color(node.color) : this.baseColor.get(mesh)!)
        material.emissive.set(node && !faded ? node.color : '#000000')
        material.emissiveIntensity = node ? 0.25 : 0
        // Switching transparency changes the shader, so it has to be recompiled.
        if (material.transparent !== faded) material.needsUpdate = true
        material.transparent = faded
        material.opacity = faded ? FADED_OPACITY : 1
        material.depthWrite = !faded
      }
    }
    this.root.traverse((object) => {
      if (object instanceof CSS2DObject) {
        const node = this.nodeOfId.get(object.userData.id)
        object.element.style.opacity = this.focused !== null && node?.key !== this.focused ? '0.15' : '1'
        object.element.style.borderColor = node ? node.color : ''
      }
    })
  }

  // ───────────────────────────────────────────────────────── camera, picking, loop

  private frameBox(box: THREE.Box3, animate: boolean) {
    const centre = box.getCenter(new THREE.Vector3())
    const size = box.getSize(new THREE.Vector3())
    const radius = Math.max(size.x, size.z, size.y * 2, 6) * 0.55
    const distance = radius / Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2))
    const to = centre.clone().add(new THREE.Vector3(0.35, 0.75, 0.9).normalize().multiplyScalar(distance))
    if (!animate) {
      this.camera.position.copy(to)
      this.controls.target.copy(centre)
      this.controls.update()
      return
    }
    this.flight = { from: this.camera.position.clone(), to, targetFrom: this.controls.target.clone(), targetTo: centre, start: performance.now() }
  }

  private handlePointerDown = (event: PointerEvent) => {
    this.pointerDown = { x: event.clientX, y: event.clientY }
  }

  private handlePointerUp = (event: PointerEvent) => {
    const down = this.pointerDown
    this.pointerDown = null
    // A drag is the camera moving, not a pick.
    if (!down || Math.hypot(event.clientX - down.x, event.clientY - down.y) > 5) return
    const rect = this.renderer.domElement.getBoundingClientRect()
    const pointer = new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1)
    const raycaster = new THREE.Raycaster()
    raycaster.setFromCamera(pointer, this.camera)
    const candidates = [...this.meshes.values()].flat().filter((m) => !(m.material as THREE.Material).transparent)
    const hit = raycaster.intersectObjects(candidates, false)[0]
    const id = hit?.object.userData.id as string | undefined
    this.onPick?.(id ? { id, kind: id.includes('-span-') ? 'span' : 'item' } : null)
  }

  private resize() {
    const { clientWidth: w, clientHeight: h } = this.container
    if (!w || !h) return
    this.renderer.setSize(w, h)
    this.labels.setSize(w, h)
    this.camera.aspect = w / h
    this.camera.updateProjectionMatrix()
  }

  private animate = () => {
    this.frame = requestAnimationFrame(this.animate)
    if (this.flight) {
      const t = Math.min(1, (performance.now() - this.flight.start) / FLY_MS)
      const ease = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2
      this.camera.position.lerpVectors(this.flight.from, this.flight.to, ease)
      this.controls.target.lerpVectors(this.flight.targetFrom, this.flight.targetTo, ease)
      if (t === 1) this.flight = null
    }
    this.controls.update()
    this.renderer.render(this.scene, this.camera)
    this.labels.render(this.scene, this.camera)
  }
}
