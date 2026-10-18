import { NavLink, Route, Routes, useMatch } from "react-router-dom";
import { Backdrop, useTiltGlare } from "./components/ui.jsx";
import Queue from "./screens/Queue.jsx";
import Project from "./screens/Project.jsx";
import AddVideo from "./screens/AddVideo.jsx";
import Verticals from "./screens/Verticals.jsx";
import Styles from "./screens/Styles.jsx";
import Taste from "./screens/Taste.jsx";
import Budget from "./screens/Budget.jsx";

export default function App() {
  const tilt = useTiltGlare();
  const proj = useMatch("/project/:id/*");
  return (
    <>
      <Backdrop cover={proj ? `/api/projects/${proj.params.id}/cover.jpg` : null} />
      <div className="scanlines" aria-hidden="true" />
      <main className="app">
        {tilt.needsPermission && (
          <button className="pill small" style={{ marginBottom: 12 }} onClick={tilt.enable}>Увімкнути відблиск від нахилу</button>
        )}
        <Routes>
          <Route path="/" element={<Queue />} />
          <Route path="/add" element={<AddVideo />} />
          <Route path="/project/:id" element={<Project />} />
          <Route path="/project/:id/vertical" element={<Verticals />} />
          <Route path="/styles" element={<Styles />} />
          <Route path="/taste" element={<Taste />} />
          <Route path="/budget" element={<Budget />} />
        </Routes>
      </main>
      <nav className="glass strong tabbar" aria-label="Розділи">
        <NavLink to="/" end><span className="ico">▤</span>Черга</NavLink>
        <NavLink to="/styles"><span className="ico">◐</span>Стилі</NavLink>
        <NavLink to="/add" className="add" aria-label="Додати відео"><span className="ico">+</span></NavLink>
        <NavLink to="/taste"><span className="ico">♡</span>Смак</NavLink>
        <NavLink to="/budget"><span className="ico">$</span>Ліміт</NavLink>
      </nav>
    </>
  );
}
