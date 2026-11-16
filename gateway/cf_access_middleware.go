// cf_access_middleware.go
//
// 네트워크 목표: Cloudflare Tunnel + Zero Trust(Access) 연동의 서버 측 보완책.
//
// Cloudflare Access는 엣지(Cloudflare 네트워크)에서 이미 인증되지 않은 사용자를 차단한다.
// 그런데 그것만 믿으면, "터널을 거치지 않고 이 서버에 직접 접근하는 경로"(같은 호스트,
// 같은 LAN, 또는 Ingress 설정 실수)가 있을 경우 Access를 완전히 우회하게 된다.
// 이 파일은 게이트웨이 자신도 "이 요청이 정말 Cloudflare Access를 통과했는가"를
// 암호학적으로 직접 검증하게 만들어, 심층 방어(defense in depth) 원칙을 네트워크
// 계층까지 확장한다.
//
// 동작 원리:
//   1) Cloudflare Access를 통과한 요청에는 "Cf-Access-Jwt-Assertion" 헤더가 자동으로 붙는다.
//   2) 이 JWT는 Cloudflare 팀 도메인의 공개키로 서명되어 있다.
//   3) 게이트웨이가 그 공개키(JWKS)를 받아와서 서명을 직접 검증하고,
//      "이 애플리케이션(aud)을 위해 발급된 게 맞는지", "아직 만료 전인지"까지 확인한다.
//
// 설정 (환경변수):
//   CF_ACCESS_TEAM_DOMAIN = "<팀이름>.cloudflareaccess.com"
//   CF_ACCESS_AUD         = "Access 애플리케이션 설정 화면에서 복사한 Audience 태그"
//
// 포인트: 두 환경변수가 비어 있으면 "로컬 전용 개발 모드"로 간주해 검증을 건너뛴다.
// 대신 반드시 경고 로그를 남겨서 "지금 인증이 꺼져 있다"는 사실을 놓치지 않게 한다.
package main

import (
	"crypto"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"math/big"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

const jwksCacheTTL = 1 * time.Hour

// jwkKey: Cloudflare의 JWKS 엔드포인트가 반환하는 개별 공개키 항목 (표준 JWK 형식 중 쓰는 필드만).
type jwkKey struct {
	Kid string `json:"kid"`
	Kty string `json:"kty"`
	N   string `json:"n"` // RSA modulus (base64url)
	E   string `json:"e"` // RSA exponent (base64url)
}

type jwksResponse struct {
	Keys []jwkKey `json:"keys"`
}

// jwksCache: kid -> rsa.PublicKey 캐시.
// 키는 자주 바뀌지 않으므로 1시간 캐싱하고, kid를 못 찾는 순간 자동으로 재조회한다.
type jwksCache struct {
	mu        sync.Mutex
	keys      map[string]*rsa.PublicKey
	fetchedAt time.Time
}

var globalJWKS = &jwksCache{}

func (c *jwksCache) getKey(kid, teamDomain string) (*rsa.PublicKey, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if key, ok := c.keys[kid]; ok && time.Since(c.fetchedAt) < jwksCacheTTL {
		return key, nil
	}

	url := fmt.Sprintf("https://%s/cdn-cgi/access/certs", teamDomain)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return nil, fmt.Errorf("Cloudflare 공개키 조회 실패: %w", err)
	}
	defer resp.Body.Close()

	var jwks jwksResponse
	if err := json.NewDecoder(resp.Body).Decode(&jwks); err != nil {
		return nil, fmt.Errorf("공개키 응답 파싱 실패: %w", err)
	}

	newKeys := make(map[string]*rsa.PublicKey, len(jwks.Keys))
	for _, k := range jwks.Keys {
		pub, err := jwkToRSAPublicKey(k.N, k.E)
		if err != nil {
			continue // 이 키 하나가 이상해도 나머지 키는 계속 쓸 수 있게 건너뛴다
		}
		newKeys[k.Kid] = pub
	}
	c.keys = newKeys
	c.fetchedAt = time.Now()

	key, ok := c.keys[kid]
	if !ok {
		return nil, fmt.Errorf("kid %q에 해당하는 공개키를 찾을 수 없습니다", kid)
	}
	return key, nil
}

// jwkToRSAPublicKey: JWK의 n(modulus)/e(exponent) 필드를 Go의 rsa.PublicKey로 변환한다.
func jwkToRSAPublicKey(nB64, eB64 string) (*rsa.PublicKey, error) {
	nBytes, err := base64.RawURLEncoding.DecodeString(nB64)
	if err != nil {
		return nil, fmt.Errorf("modulus 디코딩 실패: %w", err)
	}
	eBytes, err := base64.RawURLEncoding.DecodeString(eB64)
	if err != nil {
		return nil, fmt.Errorf("exponent 디코딩 실패: %w", err)
	}

	e := 0
	for _, b := range eBytes {
		e = e<<8 | int(b)
	}

	return &rsa.PublicKey{
		N: new(big.Int).SetBytes(nBytes),
		E: e,
	}, nil
}

// jwtHeader: JWT의 첫 번째 부분(헤더)에서 서명 검증에 필요한 값만 뽑아낸다.
type jwtHeader struct {
	Kid string `json:"kid"`
	Alg string `json:"alg"`
}

// verifyCFAccessJWT: Cloudflare Access가 발급한 JWT를 검증하고, 통과하면 claims를 반환한다.
//
// 검증 항목:
//  1. 서명이 Cloudflare 팀 도메인의 공개키로 만든 것이 맞는가 (위조/변조 방지)
//  2. aud(대상 애플리케이션)가 우리 애플리케이션과 일치하는가 (다른 앱용 토큰 재사용 방지)
//  3. exp(만료 시각)가 지나지 않았는가 (탈취된 옛 토큰 재사용 방지)
func verifyCFAccessJWT(token, teamDomain, expectedAud string) (map[string]interface{}, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, fmt.Errorf("JWT 형식이 아닙니다 (세그먼트 %d개)", len(parts))
	}
	headerB64, payloadB64, sigB64 := parts[0], parts[1], parts[2]

	headerBytes, err := base64.RawURLEncoding.DecodeString(headerB64)
	if err != nil {
		return nil, fmt.Errorf("헤더 디코딩 실패: %w", err)
	}
	var header jwtHeader
	if err := json.Unmarshal(headerBytes, &header); err != nil {
		return nil, fmt.Errorf("헤더 파싱 실패: %w", err)
	}
	if header.Alg != "RS256" {
		// alg를 고정해 "alg=none" 같은 다운그레이드 공격을 원천 차단한다.
		return nil, fmt.Errorf("지원하지 않는 서명 알고리즘입니다: %s", header.Alg)
	}

	pubKey, err := globalJWKS.getKey(header.Kid, teamDomain)
	if err != nil {
		return nil, err
	}

	sig, err := base64.RawURLEncoding.DecodeString(sigB64)
	if err != nil {
		return nil, fmt.Errorf("서명 디코딩 실패: %w", err)
	}
	signedContent := headerB64 + "." + payloadB64
	hashed := sha256.Sum256([]byte(signedContent))
	if err := rsa.VerifyPKCS1v15(pubKey, crypto.SHA256, hashed[:], sig); err != nil {
		return nil, fmt.Errorf("서명 검증 실패 (위조되었거나 손상된 토큰): %w", err)
	}

	payloadBytes, err := base64.RawURLEncoding.DecodeString(payloadB64)
	if err != nil {
		return nil, fmt.Errorf("페이로드 디코딩 실패: %w", err)
	}
	var claims map[string]interface{}
	if err := json.Unmarshal(payloadBytes, &claims); err != nil {
		return nil, fmt.Errorf("페이로드 파싱 실패: %w", err)
	}

	if !audMatches(claims["aud"], expectedAud) {
		return nil, fmt.Errorf("이 애플리케이션을 위해 발급된 토큰이 아닙니다")
	}
	if exp, ok := claims["exp"].(float64); ok {
		if time.Now().Unix() > int64(exp) {
			return nil, fmt.Errorf("토큰이 만료되었습니다")
		}
	} else {
		return nil, fmt.Errorf("exp(만료 시각) 클레임이 없습니다")
	}

	return claims, nil
}

// audMatches: aud 클레임은 문자열 하나일 수도, 문자열 배열일 수도 있다 (JWT 표준 사양).
func audMatches(audClaim interface{}, expected string) bool {
	switch v := audClaim.(type) {
	case string:
		return v == expected
	case []interface{}:
		for _, item := range v {
			if s, ok := item.(string); ok && s == expected {
				return true
			}
		}
	}
	return false
}

// withCFAccess: 미들웨어. Cloudflare Access를 통과한 요청인지 서버 자신이 한 번 더 확인한다.
// main()에서 withCORS(withCFAccess(handler)) 형태로 조합한다.
func withCFAccess(next http.HandlerFunc) http.HandlerFunc {
	teamDomain := os.Getenv("CF_ACCESS_TEAM_DOMAIN")
	aud := os.Getenv("CF_ACCESS_AUD")

	if teamDomain == "" || aud == "" {
		log.Println("[경고] CF_ACCESS_TEAM_DOMAIN/CF_ACCESS_AUD 미설정 - Access 검증이 비활성화된 " +
			"로컬 전용 모드로 동작합니다. 외부에 노출하기 전 반드시 두 환경변수를 설정하세요.")
		return next
	}

	log.Printf("[게이트웨이] Cloudflare Access 검증 활성화 (team=%s)", teamDomain)

	return func(w http.ResponseWriter, r *http.Request) {
		token := r.Header.Get("Cf-Access-Jwt-Assertion")
		if token == "" {
			http.Error(w, "Cloudflare Access 인증이 필요합니다 (터널을 거치지 않은 접근으로 보입니다)", http.StatusUnauthorized)
			return
		}

		claims, err := verifyCFAccessJWT(token, teamDomain, aud)
		if err != nil {
			log.Printf("[거부] Cloudflare Access 검증 실패: %v", err)
			http.Error(w, "인증 검증에 실패했습니다", http.StatusUnauthorized)
			return
		}

		// 감사 로그: 누가(email) 요청했는지 남긴다 - "사후 추적 가능성" 요구사항.
		if email, ok := claims["email"].(string); ok {
			log.Printf("[인증됨] %s -> %s", email, r.URL.Path)
		}

		next(w, r)
	}
}
