package io.demo.api;

@Configuration
public class WebSecurityConfig {
  public void configure(HttpSecurity http) {
    http.authorizeRequests()
        .antMatchers(HttpMethod.GET, "/articles/feed").authenticated()
        .antMatchers(HttpMethod.OPTIONS, "/**").permitAll();
  }
}
