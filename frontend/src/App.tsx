import { Navigate, Route, Routes, useParams } from "react-router-dom";

import { Layout } from "@/components/Layout";
import { Color } from "@/pages/Color";
import { Derush } from "@/pages/Derush";
import { Import } from "@/pages/Import";
import { Stabilize } from "@/pages/Stabilize";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Import />} />
        <Route path="/derush" element={<Derush />} />
        <Route path="/derush/:id" element={<Derush />} />
        <Route path="/stabilisation" element={<Stabilize />} />
        <Route path="/stabilisation/:id" element={<Stabilize />} />
        <Route path="/color" element={<Color />} />
        <Route path="/color/:id" element={<Color />} />
        {/* Old URLs, kept until bookmarks catch up. */}
        <Route path="/sequences" element={<Navigate to="/" replace />} />
        <Route path="/sequences/:id" element={<LegacySequence />} />
        <Route path="/renders" element={<Navigate to="/stabilisation" replace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

/** `/sequences/:id` used to open derush: redirect there, keeping the id. */
function LegacySequence() {
  const { id } = useParams();
  return <Navigate to={`/derush/${id}`} replace />;
}
