import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "../stores/authStore";
import { authApi } from "../services/authApi";

export function useAuth(requireAuth = true) {
  const { user, isAuthenticated, setUser, logout } = useAuthStore();
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    if (!isAuthenticated) {
      setLoading(false);
      if (requireAuth) navigate("/login");
      return;
    }

    if (!user) {
      authApi
        .getMe()
        .then((u) => {
          setUser(u);
          setLoading(false);
        })
        .catch(() => {
          logout();
          setLoading(false);
          if (requireAuth) navigate("/login");
        });
    } else {
      setLoading(false);
    }
  }, [isAuthenticated, user, requireAuth, navigate, setUser, logout]);

  return { user, loading, isAuthenticated };
}
